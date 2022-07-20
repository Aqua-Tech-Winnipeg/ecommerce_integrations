import frappe
from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return
from frappe.utils import cint, cstr, getdate, nowdate
from typing import List

from ecommerce_integrations.shopify.constants import (
	ORDER_ID_FIELD,
	ORDER_NUMBER_FIELD,
	SETTING_DOCTYPE,
)
from ecommerce_integrations.shopify.utils import create_shopify_log
from ecommerce_integrations.shopify.product import get_item_code


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
	credit_note = create_credit_note(sales_invoice.name)

	if not refund["restock"]:
		credit_note.update_stock = 0

	return_items = [get_item_code(line.get("line_item")) for line in refund.get("refund_line_items")]

	_handle_partial_returns(credit_note, return_items)

	credit_note.insert(ignore_mandatory=True)
	credit_note.submit()


def create_credit_note(invoice_name):
	credit_note = make_sales_return(invoice_name)
	
	for item in credit_note.items:
		item.warehouse = setting.warehouse or item.warehouse

	for tax in credit_note.taxes:
		tax.item_wise_tax_detail = json.loads(tax.item_wise_tax_detail)
		for item, tax_distribution in tax.item_wise_tax_detail.items():
			tax_distribution[1] *= -1
		tax.item_wise_tax_detail = json.dumps(tax.item_wise_tax_detail)
	
	return credit_note

def get_sales_invoice(order_id):
	"""Get ERPNext sales invoice using shopify order id."""
	sales_invoice = frappe.db.get_value("Sales Invoice", filters={ORDER_ID_FIELD: order_id})
	if sales_invoice:
		return frappe.get_doc("Sales Invoice", sales_invoice)

def _handle_partial_returns(credit_note, returned_items: List[str]) -> None:
	""" Remove non-returned item from credit note and update taxes """

	item_code_to_qty_map = defaultdict(float)
	for item in credit_note.items:
		item_code_to_qty_map[item.item_code] += item.qty

	# remove non-returned items
	credit_note.items = [
		item for item in credit_note.items if item.sales_invoice_item in returned_items
	]

	returned_qty_map = defaultdict(float)
	for item in credit_note.items:
		returned_qty_map[item.item_code] += item.qty

	for tax in credit_note.taxes:
		# reduce total value
		item_wise_tax_detail = json.loads(tax.item_wise_tax_detail)
		new_tax_amt = 0.0

		for item_code, tax_distribution in item_wise_tax_detail.items():
			# item_code: [rate, amount]
			if not tax_distribution[1]:
				# Ignore 0 values
				continue
			return_percent = returned_qty_map.get(item_code, 0.0) / item_code_to_qty_map.get(item_code)
			tax_distribution[1] *= return_percent
			new_tax_amt += tax_distribution[1]

		tax.tax_amount = new_tax_amt
		tax.item_wise_tax_detail = json.dumps(item_wise_tax_detail)
