import json
from collections import defaultdict

import frappe
from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return
from frappe.utils import cint, cstr, getdate, nowdate
from typing import DefaultDict, List

from ecommerce_integrations.shopify.constants import (
	ORDER_ID_FIELD,
	ORDER_NUMBER_FIELD,
	SETTING_DOCTYPE,
)
from ecommerce_integrations.shopify.utils import create_shopify_log
from ecommerce_integrations.shopify.product import get_item_code
from ecommerce_integrations.shopify.invoice import make_payament_entry_against_sales_invoice


def prepare_credit_note(payload, request_id=None):
	refund = payload
	frappe.set_user("Administrator")
	setting = frappe.get_doc(SETTING_DOCTYPE)
	frappe.flags.request_id = request_id

	try:
		sales_invoice = get_sales_invoice(cstr(refund["order_id"]))
		if sales_invoice:
			make_credit_note(refund, setting, sales_invoice)
			create_shopify_log(status="Success")
		else:
			create_shopify_log(status="Invalid", message="Sales Invoice not found for creating Credit Note.")
	except Exception as e:
		create_shopify_log(status="Error", exception=e, rollback=True)

def make_credit_note(refund, setting, sales_invoice):
	if len(refund.get("refund_line_items")) > 0:
		credit_note = create_credit_note(sales_invoice.name, setting)

		if not refund["restock"]:
			credit_note.update_stock = 0

		#return_items = [get_item_code(line.get("line_item")) for line in refund.get("refund_line_items")]
		return_items = defaultdict()
		for line in refund.get("refund_line_items"):
			return_items[get_item_code(line.get("line_item"))] = {"qty": line.get("quantity"), "price": line.get("subtotal")}


		_handle_partial_returns(credit_note, return_items, sales_invoice)

		credit_note.insert(ignore_mandatory=True)
		credit_note.submit()
		
	
	if len(refund.get("order_adjustments")) > 0:
		amount = sum(float(adjustment.get("amount")) + float(adjustment.get("tax_amount")) for adjustment in refund.get("order_adjustments"))
		debit_note = create_debit_note(sales_invoice, amount, setting)
		debit_note.insert(ignore_mandatory=True)
		debit_note.submit()
	make_payament_entry_against_sales_invoice(sales_invoice, setting)

def create_debit_note(sales_invoice, amount, setting):
	debit_note = create_credit_note(sales_invoice.name, setting)
	debit_note.is_debit_note = 1

	original_amount = sum((item.rate * item.qty) for item in debit_note.items) + debit_note.total_taxes_and_charges


	for item in debit_note.items:
		item_percent = (item.rate * item.qty) / original_amount
		item.rate =  -item_percent * amount
		item.qty = 0

	for tax in debit_note.taxes:
		# reduce total value
		item_wise_tax_detail = json.loads(tax.item_wise_tax_detail)

		for item_code, tax_distribution in item_wise_tax_detail.items():
			# item_code: [rate, amount]
			if not tax_distribution[1]:
				# Ignore 0 values
				continue
			return_percent = amount / original_amount
			tax_distribution[0] *= return_percent
			tax_distribution[1] *= return_percent

		tax.tax_amount *= amount / original_amount
		tax.item_wise_tax_detail = json.dumps(item_wise_tax_detail)
	return debit_note



def create_credit_note(invoice_name, setting):
	credit_note = make_sales_return(invoice_name)
	
	for item in credit_note.items:
		item.warehouse = setting.warehouse or item.warehouse

	for tax in credit_note.taxes:
		tax.item_wise_tax_detail = json.loads(tax.item_wise_tax_detail)
		for item, tax_distribution in tax.item_wise_tax_detail.items():
			tax_distribution[1] *= -1
			tax_distribution[0] *= -1
		tax.item_wise_tax_detail = json.dumps(tax.item_wise_tax_detail)
	
	return credit_note

def get_sales_invoice(order_id):
	"""Get ERPNext sales invoice using shopify order id."""
	sales_invoice = frappe.db.get_value("Sales Invoice", filters={ORDER_ID_FIELD: order_id})
	if sales_invoice:
		return frappe.get_doc("Sales Invoice", sales_invoice)

def _handle_partial_returns(credit_note, returned_items: List[str], sales_invoice) -> None:
	""" Remove non-returned item from credit note and update taxes """

	item_code_to_qty_map = defaultdict(float)
	for item in sales_invoice.items:
		item_code_to_qty_map[item.item_code] -= item.qty

	# remove non-returned items
	credit_note.items = [
		item for item in credit_note.items if item.item_code in returned_items
	]
	for item in credit_note.items:
		item.qty = -1 * returned_items[item.item_code]["qty"]
		item.amount = -1 * returned_items[item.item_code]["price"]
		item.rate = returned_items[item.item_code]["price"] / returned_items[item.item_code]["qty"]

	returned_qty_map = defaultdict(float)
	for item in credit_note.items:
		returned_qty_map[item.item_code] += item.qty

	for tax in credit_note.taxes:
		# reduce total value
		item_wise_tax_detail = json.loads(tax.item_wise_tax_detail)
		new_tax_amt = 0.0

		for item_code, tax_distribution in item_wise_tax_detail.items():
			# item_code: [rate, amount]
			if not tax_distribution[0]:
				# Ignore 0 values
				continue
			return_percent = returned_qty_map.get(item_code, 0.0) / item_code_to_qty_map.get(item_code)
			tax_distribution[1] *= return_percent
			new_tax_amt += tax_distribution[1]

		tax.tax_amount = new_tax_amt
		tax.item_wise_tax_detail = json.dumps(item_wise_tax_detail)
