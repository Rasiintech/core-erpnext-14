# Copyright (c) 2026, Frappe Technologies Pvt. Ltd.
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import getdate, flt, get_first_day, nowdate
from datetime import datetime, date, time, timedelta

# ============================================================
# Safe helpers
# ============================================================
def _has_field(doctype: str, fieldname: str) -> bool:
    try:
        return frappe.get_meta(doctype).has_field(fieldname)
    except Exception:
        return False


def _inv_item_amount_field():
    # You said: use net_amount (not base). This returns net_amount if it exists.
    return "net_amount" if _has_field("Sales Invoice Item", "net_amount") else "amount"


def _cp_date_field():
    return "completed_date" if _has_field("Clinical Procedure", "completed_date") else "modified"


def _datetime_bounds(from_date, to_date):
    # from_date/to_date are date objects
    start = datetime.combine(from_date, time.min)  # 00:00:00
    end = datetime.combine(to_date + timedelta(days=1), time.min)  # next day 00:00:00
    return start, end


def _rule_has_source_order():
    # Field in Commission Profile Rule
    return _has_field("Commission Profile Rule", "source_order")


def _si_has_source_order():
    return _has_field("Sales Invoice", "source_order")


def _si_has_inpatient_record():
    return _has_field("Sales Invoice", "inpatient_record")


def _as_date(v):
    if not v:
        return date.min
    if isinstance(v, datetime):
        return v.date()
    return v


def _normalize_str(v) -> str:
    return (v or "").strip()


def _si_effective_source_order(si_source_order, si_inpatient_record):
    """
    Effective Source Order:
      1) If Sales Invoice.source_order is set -> use it
      2) Else if inpatient_record exists and set -> IPD
      3) Else -> OPD  (DEFAULT)
    """
    so = _normalize_str(si_source_order)
    if so:
        return so

    if _si_has_inpatient_record():
        if si_inpatient_record:
            return "IPD"

    # Default fallback
    return "OPD"


def _matches_source_order(rule_source_order: str, invoice_effective_source_order: str) -> bool:
    """
    Match logic:
      - rule.source_order empty -> match ANY
      - rule.source_order == "Any" (case-insensitive) -> match ANY
      - otherwise -> must equal invoice effective source order
    """
    rs = _normalize_str(rule_source_order)
    if not rs:
        return True
    if rs.lower() == "any":
        return True
    return rs == _normalize_str(invoice_effective_source_order)

def _commission_sign(base_amount, is_return=False) -> float:
    # If base is negative (common in returns), commission must be negative
    if flt(base_amount) < 0:
        return -1.0
    # If ERPNext marks invoice as return, also force negative even if base is positive
    if is_return:
        return -1.0
    return 1.0

def _should_force_doctor_scope():
    roles = set(frappe.get_roles(frappe.session.user) or [])

    privileged_roles = {
        "System Manager",
        "Accounts Manager",
        "Auditor",
    }

    if roles.intersection(privileged_roles):
        return False

    return "Doctor" in roles


def _get_logged_in_practitioner():
    practitioner = frappe.db.get_value(
        "Healthcare Practitioner",
        {"user_id": frappe.session.user},
        "name"
    )
    if practitioner:
        return practitioner

    practitioners = frappe.get_all(
        "User Permission",
        filters={
            "user": frappe.session.user,
            "allow": "Healthcare Practitioner"
        },
        pluck="for_value",
        limit=2,
    )

    if len(practitioners) == 1:
        return practitioners[0]

    if len(practitioners) > 1:
        frappe.throw(_("This user has multiple Healthcare Practitioner user permissions. Please keep only one."))

    return None


def _enforce_doctor_filters(filters):
    """
    Server-side protection:
    - Normal doctor users can only see their own practitioner data
    - Management users can see all
    - Doctors cannot see data before 2026-03-01
    """
    MIN_DOCTOR_FROM_DATE = getdate("2026-03-01")

    from_date = getdate(filters.get("from_date")) if filters.get("from_date") else MIN_DOCTOR_FROM_DATE
    to_date = getdate(filters.get("to_date")) if filters.get("to_date") else getdate(nowdate())

    if _should_force_doctor_scope():
        if from_date < MIN_DOCTOR_FROM_DATE:
            from_date = MIN_DOCTOR_FROM_DATE

        practitioner = _get_logged_in_practitioner()
        if not practitioner:
            frappe.throw(_("This doctor user does not have a linked or permitted Healthcare Practitioner."))

        filters["receiver_practitioner"] = practitioner

    if to_date < from_date:
        to_date = from_date

    filters["from_date"] = from_date
    filters["to_date"] = to_date

    return filters
# ============================================================
# Load Commission Profiles (enabled)
# ============================================================
def _get_profiles(filters):
    prof_filters = {"disabled": 0} if _has_field("Commission Profile", "disabled") else {}

    # We keep only practitioner filter; employee receiver filter removed as requested
    if filters.get("receiver_practitioner"):
        prof_filters["practitioner"] = filters["receiver_practitioner"]

    return frappe.get_all(
        "Commission Profile",
        filters=prof_filters,
        fields=["name", "practitioner", "employee", "disabled"],
        order_by="name asc",
    )


def _get_profile_rules(profile_name: str):
    """
    Child table doctype: "Commission Profile Rule"
    Must have:
      item_group, rule_type, applies_when, percent, amount, stackable, priority
    Optional:
      source_order  (Link/Data)
    """
    fields = [
        "name",
        "item_group",
        "rule_type",
        "applies_when",
        "percent",
        "amount",
        "stackable",
        "priority",
    ]
    if _rule_has_source_order():
        fields.append("source_order")

    rules = frappe.get_all(
        "Commission Profile Rule",
        filters={"parent": profile_name, "parenttype": "Commission Profile"},
        fields=fields,
        order_by="priority asc, idx asc",
    )

    for r in rules:
        r["stackable"] = 1 if r.get("stackable") else 0
        r["priority"] = int(r.get("priority") or 0)
        if _rule_has_source_order():
            r["source_order"] = _normalize_str(r.get("source_order"))

    return rules


# ============================================================
# Matching conditions
# ============================================================
def _match_invoice_rule(rule, receiver_type, receiver_practitioner, invoice_requester):
    cond = _normalize_str(rule.get("applies_when") or "Any")

    if cond == "Any":
        return True

    # requester-based rules only meaningful for practitioner receivers
    if receiver_type != "Practitioner":
        return False

    if cond == "Requester":
        return _normalize_str(invoice_requester) == _normalize_str(receiver_practitioner)

    if cond == "Requester != Receiver":
        return bool(invoice_requester) and _normalize_str(invoice_requester) != _normalize_str(receiver_practitioner)

    return False


def _match_procedure_rule(rule, receiver_type, receiver_practitioner, req, perf, anesthesia_practitioner):
    """
    NOTE: anesthesia_practitioner is Healthcare Practitioner (Link)
    """
    cond = _normalize_str(rule.get("applies_when") or "Any")

    if cond == "Any":
        return True

    if cond in ("Requester", "Requester != Receiver", "Requester = Performer", "Requester Only", "Performer Only"):
        if receiver_type != "Practitioner":
            return False

        req = _normalize_str(req)
        perf = _normalize_str(perf)
        rp = _normalize_str(receiver_practitioner)

        if cond == "Requester":
            return req == rp

        if cond == "Requester != Receiver":
            return bool(req) and req != rp

        if cond == "Requester = Performer":
            return req == rp and perf == rp

        if cond == "Requester Only":
            return req == rp and perf != rp

        if cond == "Performer Only":
            return perf == rp and req != rp

    # UPDATED: anesthesia matches practitioner directly (no employee lookup)
    if cond in ("Anesthesia", "Anaesthesia"):
        return _normalize_str(anesthesia_practitioner) == _normalize_str(receiver_practitioner)

    return False


# ============================================================
# Net % helpers (Requester minus Performer)
# ============================================================
def _sum_rule_percent(rules):
    """Sum percent for Procedure Percent rules (after pick_rules)."""
    total = 0.0
    for r in rules or []:
        total += flt(r.get("percent"))
    return total


def _get_practitioner_profile(practitioner: str):
    """Return first enabled Commission Profile for a practitioner."""
    if not practitioner:
        return None

    prof_filters = {"practitioner": practitioner}
    if _has_field("Commission Profile", "disabled"):
        prof_filters["disabled"] = 0

    p = frappe.get_all(
        "Commission Profile",
        filters=prof_filters,
        fields=["name"],
        order_by="name asc",
        limit=1,
    )
    return p[0]["name"] if p else None


def _get_performer_percent_for_cp(cp, item_group, eff_so, _cache):
    """
    Find performer commission percent from performer's profile
    using Procedure Percent + Performer Only logic, matching item_group + source_order.
    """
    perf = _normalize_str(cp.get("performing_practitioner"))
    req = _normalize_str(cp.get("requesting_practitioner"))
    if not perf or perf == req:
        return 0.0

    cache_key = (perf, item_group, eff_so)
    if cache_key in _cache:
        return _cache[cache_key]

    prof = _get_practitioner_profile(perf)
    if not prof:
        _cache[cache_key] = 0.0
        return 0.0

    perf_rules = _get_profile_rules(prof) or []

    applicable = []
    for rule in perf_rules:
        if _normalize_str(rule.get("rule_type")) != "Procedure Percent":
            continue
        if _normalize_str(rule.get("item_group")) != _normalize_str(item_group):
            continue
        if _rule_has_source_order() and not _matches_source_order(rule.get("source_order"), eff_so):
            continue

        # This will only match rules that are actually "Performer Only"
        if _match_procedure_rule(
            rule,
            "Practitioner",
            perf,
            cp.get("requesting_practitioner"),
            cp.get("performing_practitioner"),
            cp.get("anesthesia_practitioner"),   # UPDATED
        ):
            applicable.append(rule)

    final_rules = _pick_rules(applicable)
    pct = _sum_rule_percent(final_rules)

    _cache[cache_key] = pct
    return pct


# ============================================================
# Commission calc + picking rules
# ============================================================
# def _calc_commission(rule, base_amount):
#     rt = _normalize_str(rule.get("rule_type"))
#     if rt in ("Invoice Percent", "Procedure Percent"):
#         return flt(base_amount) * flt(rule.get("percent")) / 100.0
#     if rt == "Invoice Fixed":
#         return flt(rule.get("amount"))
#     return 0.0

def _calc_commission(rule, base_amount, sign: float = 1.0):
    rt = _normalize_str(rule.get("rule_type"))
    base = flt(base_amount)
    s = flt(sign) or 1.0

    if rt in ("Invoice Percent", "Procedure Percent"):
        # Use abs(base) then apply sign so returns always become negative
        return abs(base) * flt(rule.get("percent")) / 100.0 * s

    if rt == "Invoice Fixed":
        return flt(rule.get("amount")) * s

    return 0.0

def _pick_rules(applicable):
    """
    Apply all stackable rules + first non-stackable rule (if any).
    """
    stackable = [r for r in applicable if r.get("stackable")]
    non_stackable = [r for r in applicable if not r.get("stackable")]
    final = []
    final.extend(stackable)
    if non_stackable:
        final.append(non_stackable[0])
    return final


# ============================================================
# Data loaders
# ============================================================
def _load_invoice_lines(from_date, to_date, filters):
    if not _has_field("Sales Invoice", "ref_practitioner"):
        frappe.throw(_("Sales Invoice.ref_practitioner field not found."))

    amt = _inv_item_amount_field()

    item_group_filter = _normalize_str(filters.get("item_group"))
    source_order_filter = _normalize_str(filters.get("source_order"))

    select_so = ", si.source_order AS si_source_order" if _si_has_source_order() else ", '' AS si_source_order"
    select_ipd = ", si.inpatient_record AS si_inpatient_record" if _si_has_inpatient_record() else ", '' AS si_inpatient_record"

    rows = frappe.db.sql(
        f"""
        SELECT
            si.name AS invoice,
            si.posting_date AS posting_date,
            si.is_return AS is_return,
            si.ref_practitioner AS requesting_practitioner
            {select_so}
            {select_ipd},
            sii.name AS invoice_item,
            sii.item_code,
            sii.item_name,
            COALESCE(sii.{amt}, 0) AS base_amount,
            it.item_group
        FROM `tabSales Invoice` si
        INNER JOIN `tabSales Invoice Item` sii ON sii.parent = si.name
        LEFT JOIN `tabItem` it ON it.name = sii.item_code
        WHERE si.docstatus = 1
          AND si.posting_date BETWEEN %(from_date)s AND %(to_date)s
        """,
        {"from_date": from_date, "to_date": to_date},
        as_dict=True,
    )

    out = []
    for r in rows:
        ig = _normalize_str(r.get("item_group"))
        if item_group_filter and ig != item_group_filter:
            continue

        eff_so = _si_effective_source_order(r.get("si_source_order"), r.get("si_inpatient_record"))
        r["effective_source_order"] = eff_so

        # IMPORTANT: report filter source_order also supports "Any"
        if source_order_filter and source_order_filter.lower() != "any" and eff_so != source_order_filter:
            continue

        out.append(r)

    return out


def _load_completed_procedures(from_date, to_date, filters):
    # UPDATED: require anesthesia_practitioner instead of anesthetist_practitioner
    required = ["sales_invoice_item", "practitioner", "performing_practitioner", "anesthesia_practitioner", "status"]
    for f in required:
        if not _has_field("Clinical Procedure", f):
            frappe.throw(_("Clinical Procedure.{0} field not found.").format(f))

    cp_date = _cp_date_field()
    has_sales_invoice = _has_field("Clinical Procedure", "sales_invoice")
    has_template = _has_field("Clinical Procedure", "procedure_template")

    item_group_filter = _normalize_str(filters.get("item_group"))
    source_order_filter = _normalize_str(filters.get("source_order"))

    from_dt, to_dt = _datetime_bounds(from_date, to_date)

    cps = frappe.db.sql(
        f"""
        SELECT
            cp.name,
            cp.{cp_date} AS done_date,
            cp.practitioner AS requesting_practitioner,
            cp.performing_practitioner,
            cp.anesthesia_practitioner,
            cp.sales_invoice_item
            {", cp.sales_invoice" if has_sales_invoice else ""}
            {", cp.procedure_template" if has_template else ""},
            it.item_group AS procedure_item_group
        FROM `tabClinical Procedure` cp
        LEFT JOIN `tabItem` it ON it.name = cp.procedure_template
        WHERE cp.status = 'Completed'
          AND cp.{cp_date} >= %(from_dt)s
          AND cp.{cp_date} < %(to_dt)s
        """,
        {"from_dt": from_dt, "to_dt": to_dt},
        as_dict=True,
    )

    out = []
    for cp in cps:
        ig = _get_procedure_item_group(cp)
        if item_group_filter and ig != item_group_filter:
            continue

        base, inv, eff_so = _get_ot_base_amount_and_source_order(cp)
        cp["__base_amount_cached"] = flt(base)
        cp["__invoice_cached"] = inv
        cp["__effective_source_order_cached"] = eff_so

        # report filter source_order also supports "Any"
        if source_order_filter and source_order_filter.lower() != "any" and _normalize_str(eff_so) != source_order_filter:
            continue

        out.append(cp)

    return out


def _get_procedure_item_group(cp_row):
    ig = _normalize_str(cp_row.get("procedure_item_group"))
    if ig:
        return ig
    return "OT"


def _get_ot_base_amount_and_source_order(cp_row):
    """
    Returns: (base_amount, invoice_name, effective_source_order)

    base_amount: from Sales Invoice Item (net_amount/amount)
    effective_source_order: invoice.source_order else inpatient_record->IPD else OPD
    """
    amt = _inv_item_amount_field()

    base = 0.0
    ref_invoice = None
    eff_so = "OPD"  # default

    def _compute_eff_so(si_source_order, si_inpatient_record):
        return _si_effective_source_order(si_source_order, si_inpatient_record)

    if cp_row.get("sales_invoice_item"):
        select_so = "si.source_order AS si_source_order," if _si_has_source_order() else "'' AS si_source_order,"
        select_ipd = "si.inpatient_record AS si_inpatient_record" if _si_has_inpatient_record() else "'' AS si_inpatient_record"

        r = frappe.db.sql(
            f"""
            SELECT
                sii.parent AS invoice,
                si.docstatus AS inv_docstatus,
                COALESCE(sii.{amt}, 0) AS base_amount,
                {select_so}
                {select_ipd}
            FROM `tabSales Invoice Item` sii
            INNER JOIN `tabSales Invoice` si ON si.name = sii.parent
            WHERE sii.name = %(sii)s
            """,
            {"sii": cp_row["sales_invoice_item"]},
            as_dict=True,
        )
        if r and r[0].get("inv_docstatus") == 1:
            base = flt(r[0].get("base_amount"))
            ref_invoice = r[0].get("invoice")
            eff_so = _compute_eff_so(r[0].get("si_source_order"), r[0].get("si_inpatient_record"))

    if base == 0 and cp_row.get("sales_invoice") and cp_row.get("procedure_template"):
        select_so = ", si.source_order AS si_source_order" if _si_has_source_order() else ", '' AS si_source_order"
        select_ipd = ", si.inpatient_record AS si_inpatient_record" if _si_has_inpatient_record() else ", '' AS si_inpatient_record"

        r2 = frappe.db.sql(
            f"""
            SELECT
                COALESCE(SUM(COALESCE(sii.{amt}, 0)), 0) AS base_amount
                {select_so}
                {select_ipd}
            FROM `tabSales Invoice` si
            INNER JOIN `tabSales Invoice Item` sii ON sii.parent = si.name
            WHERE si.name = %(inv)s
              AND si.docstatus = 1
              AND sii.item_code = %(item)s
            """,
            {"inv": cp_row["sales_invoice"], "item": cp_row["procedure_template"]},
            as_dict=True,
        )
        if r2:
            base = flt(r2[0].get("base_amount"))
            ref_invoice = cp_row.get("sales_invoice")
            eff_so = _compute_eff_so(r2[0].get("si_source_order"), r2[0].get("si_inpatient_record"))

    return base, ref_invoice, eff_so


# ============================================================
# Report
# ============================================================

def execute(filters=None):
    filters = filters or {}
    filters = _enforce_doctor_filters(filters)

    from_date = getdate(filters.get("from_date"))
    to_date = getdate(filters.get("to_date"))

    view = (filters.get("view") or "Top Earners").strip()

    profiles = _get_profiles(filters)

    receivers = []
    for p in profiles:
        receiver_practitioner = p.get("practitioner")
        if receiver_practitioner:
            receivers.append({
                "profile": p["name"],
                "receiver_type": "Practitioner",
                "receiver_practitioner": receiver_practitioner,
                "receiver_employee": None,
                "receiver_name": receiver_practitioner,
            })

    invoice_lines = _load_invoice_lines(from_date, to_date, filters)
    cps = _load_completed_procedures(from_date, to_date, filters)

    if view == "Details":
        columns = get_columns_details()
        rows, totals = build_details(receivers, invoice_lines, cps, filters)
        summary, chart = make_kpis_and_chart(totals, details_mode=True)
        return columns, rows, None, chart, summary

    if view == "By Item Group":
        columns = get_columns_item_group_summary()
        rows, totals = build_item_group_summary(receivers, invoice_lines, cps, filters)
        summary, chart = make_kpis_and_chart(totals, details_mode=True)
        return columns, rows, None, chart, summary

    columns = get_columns_summary()
    rows, totals = build_summary(receivers, invoice_lines, cps, filters)
    summary, chart = make_kpis_and_chart(totals, details_mode=False)
    return columns, rows, None, chart, summary

# def execute(filters=None):
#     filters = filters or {}

#     from_date = getdate(filters.get("from_date"))
#     to_date = filters.get("to_date")
#     to_date = getdate(to_date) if to_date else from_date  # allow single-day filtering

#     view = (filters.get("view") or "Top Earners").strip()

#     profiles = _get_profiles(filters)

#     # UPDATED: only practitioner receivers (employee receivers ignored)
#     receivers = []
#     for p in profiles:
#         receiver_practitioner = p.get("practitioner")
#         if receiver_practitioner:
#             receivers.append({
#                 "profile": p["name"],
#                 "receiver_type": "Practitioner",
#                 "receiver_practitioner": receiver_practitioner,
#                 "receiver_employee": None,
#                 "receiver_name": receiver_practitioner,
#             })

#     invoice_lines = _load_invoice_lines(from_date, to_date, filters)
#     cps = _load_completed_procedures(from_date, to_date, filters)

#     if view == "Details":
#         columns = get_columns_details()
#         rows, totals = build_details(receivers, invoice_lines, cps, filters)
#         summary, chart = make_kpis_and_chart(totals, details_mode=True)
#         return columns, rows, None, chart, summary

#     if view == "By Item Group":
#         columns = get_columns_item_group_summary()
#         rows, totals = build_item_group_summary(receivers, invoice_lines, cps, filters)
#         summary, chart = make_kpis_and_chart(totals, details_mode=True)
#         return columns, rows, None, chart, summary

#     columns = get_columns_summary()
#     rows, totals = build_summary(receivers, invoice_lines, cps, filters)
#     summary, chart = make_kpis_and_chart(totals, details_mode=False)
#     return columns, rows, None, chart, summary


# ============================================================
# Columns
# ============================================================
def get_columns_summary():
    return [
        {"label": _("Receiver Type"), "fieldname": "receiver_type", "fieldtype": "Data", "width": 120},
        {"label": _("Receiver"), "fieldname": "receiver", "fieldtype": "Dynamic Link", "options": "receiver_doctype", "width": 220},
        {"label": _("Total Commission"), "fieldname": "total_commission", "fieldtype": "Currency", "width": 140},
        {"label": _("Transactions"), "fieldname": "tx_count", "fieldtype": "Int", "width": 110},
        {"label": _("Invoice Commission"), "fieldname": "invoice_commission", "fieldtype": "Currency", "width": 150},
        {"label": _("Procedure Commission"), "fieldname": "procedure_commission", "fieldtype": "Currency", "width": 160},
    ]


def get_columns_details():
    return [
        {"label": _("Date"), "fieldname": "date", "fieldtype": "Date", "width": 95},
        {"label": _("Reference Type"), "fieldname": "reference_type", "fieldtype": "Data", "width": 140},
        {"label": _("Reference"), "fieldname": "reference", "fieldtype": "Dynamic Link", "options": "reference_type", "width": 170},
        {"label": _("Item / Procedure"), "fieldname": "item_or_procedure", "fieldtype": "Data", "width": 240},
        {"label": _("Item Group"), "fieldname": "item_group", "fieldtype": "Link", "options": "Item Group", "width": 160},
        {"label": _("Scenario"), "fieldname": "scenario", "fieldtype": "Data", "width": 170},
        {"label": _("Source Order"), "fieldname": "source_order", "fieldtype": "Data", "width": 120},
        {"label": _("Requesting"), "fieldname": "requesting_practitioner", "fieldtype": "Data", "width": 140},
        {"label": _("Performing"), "fieldname": "performing_practitioner", "fieldtype": "Data", "width": 140},
        # UPDATED: anesthesia is Healthcare Practitioner
        {"label": _("Anesthesia"), "fieldname": "anesthesia_practitioner", "fieldtype": "Data", "width": 160},
        {"label": _("Base Amount"), "fieldname": "base_amount", "fieldtype": "Currency", "width": 120},
        {"label": _("Rule"), "fieldname": "rule", "fieldtype": "Data", "width": 320},
        {"label": _("Commission"), "fieldname": "commission_amount", "fieldtype": "Currency", "width": 120},
    ]


def get_columns_item_group_summary():
    return [
        {"label": _("Item Group"), "fieldname": "item_group", "fieldtype": "Link", "options": "Item Group", "width": 180},
        {"label": _("Scenario"), "fieldname": "scenario", "fieldtype": "Data", "width": 170},
        {"label": _("Source Order"), "fieldname": "source_order", "fieldtype": "Data", "width": 120},
        {"label": _("Total Amount"), "fieldname": "base_total", "fieldtype": "Currency", "width": 140},
        {"label": _("Net Commission"), "fieldname": "commission_total", "fieldtype": "Currency", "width": 140},
        {"label": _("Commission %"), "fieldname": "effective_pct", "fieldtype": "Percent", "width": 120},
    ]


# ============================================================
# Build summary (Top Earners)
# ============================================================
def build_summary(receivers, invoice_lines, cps, filters):
    totals = {
        "total_commission": 0.0,
        "invoice_commission": 0.0,
        "procedure_commission": 0.0,
        "tx_count": 0,
        "by_receiver": {},
    }

    rules_map = {r["profile"]: _get_profile_rules(r["profile"]) for r in receivers}
    performer_pct_cache = {}

    def add(receiver_key, receiver_type, receiver_doctype, receiver_name, inv_amt=0.0, proc_amt=0.0, tx_inc=0):
        rec = totals["by_receiver"].setdefault(receiver_key, {
            "receiver_type": receiver_type,
            "receiver_doctype": receiver_doctype,
            "receiver": receiver_name,
            "invoice_commission": 0.0,
            "procedure_commission": 0.0,
            "total_commission": 0.0,
            "tx_count": 0,
        })

        rec["invoice_commission"] += inv_amt
        rec["procedure_commission"] += proc_amt
        rec["total_commission"] += (inv_amt + proc_amt)
        rec["tx_count"] += tx_inc

        totals["invoice_commission"] += inv_amt
        totals["procedure_commission"] += proc_amt
        totals["total_commission"] += (inv_amt + proc_amt)
        totals["tx_count"] += tx_inc

    # Invoice pass
    for rec in receivers:
        rules = rules_map.get(rec["profile"]) or []
        receiver_type = rec["receiver_type"]
        receiver_prac = rec.get("receiver_practitioner")

        receiver_key = f"{receiver_type}:{receiver_prac}"
        receiver_doctype = "Healthcare Practitioner"

        for line in invoice_lines:
            ig = _normalize_str(line.get("item_group"))
            if not ig:
                continue

            inv_so = _normalize_str(line.get("effective_source_order")) or "OPD"

            applicable = []
            for rule in rules:
                if _normalize_str(rule.get("rule_type")) not in ("Invoice Percent", "Invoice Fixed"):
                    continue
                if _normalize_str(rule.get("item_group")) != ig:
                    continue
                if _rule_has_source_order() and not _matches_source_order(rule.get("source_order"), inv_so):
                    continue
                if _match_invoice_rule(rule, receiver_type, receiver_prac, line.get("requesting_practitioner")):
                    applicable.append(rule)

            final_rules = _pick_rules(applicable)
            if not final_rules:
                continue

            base = flt(line.get("base_amount"))
            sign = _commission_sign(base, line.get("is_return"))
            for rule in final_rules:
                c = _calc_commission(rule, base, sign=sign)
                if c:
                    add(receiver_key, receiver_type, receiver_doctype, rec["receiver_name"], inv_amt=c, tx_inc=1)

    # Procedure pass (net requester % when requester/requester-only)
    for rec in receivers:
        rules = rules_map.get(rec["profile"]) or []
        receiver_type = rec["receiver_type"]
        receiver_prac = rec.get("receiver_practitioner")

        receiver_key = f"{receiver_type}:{receiver_prac}"
        receiver_doctype = "Healthcare Practitioner"

        for cp in cps:
            ig = _get_procedure_item_group(cp)
            eff_so = _normalize_str(cp.get("__effective_source_order_cached")) or "OPD"

            applicable = []
            for rule in rules:
                if _normalize_str(rule.get("rule_type")) != "Procedure Percent":
                    continue
                if _normalize_str(rule.get("item_group")) != ig:
                    continue
                if _rule_has_source_order() and not _matches_source_order(rule.get("source_order"), eff_so):
                    continue

                if _match_procedure_rule(
                    rule,
                    receiver_type,
                    receiver_prac,
                    cp.get("requesting_practitioner"),
                    cp.get("performing_practitioner"),
                    cp.get("anesthesia_practitioner"),  # UPDATED
                ):
                    applicable.append(rule)

            final_rules = _pick_rules(applicable)
            if not final_rules:
                continue

            base = flt(cp.get("__base_amount_cached") or 0)
            requester_pct = _sum_rule_percent(final_rules)

            subtract_set = {"Requester", "Requester Only"}
            scenario_hit = any(_normalize_str(r.get("applies_when")) in subtract_set for r in final_rules)

            net_pct = requester_pct
            if (
                receiver_type == "Practitioner"
                and _normalize_str(cp.get("requesting_practitioner")) == _normalize_str(receiver_prac)
                and _normalize_str(cp.get("performing_practitioner"))
                and _normalize_str(cp.get("performing_practitioner")) != _normalize_str(receiver_prac)
                and scenario_hit
            ):
                perf_pct = _get_performer_percent_for_cp(cp, ig, eff_so, performer_pct_cache)
                net_pct = max(0.0, requester_pct - perf_pct)

            c = flt(base) * flt(net_pct) / 100.0
            if c:
                add(receiver_key, receiver_type, receiver_doctype, rec["receiver_name"], proc_amt=c, tx_inc=1)

    rows = list(totals["by_receiver"].values())
    rows.sort(key=lambda d: flt(d.get("total_commission")), reverse=True)
    return rows, totals


# ============================================================
# Build details
# ============================================================
def build_details(receivers, invoice_lines, cps, filters):
    totals = {
        "total_commission": 0.0,
        "invoice_commission": 0.0,
        "procedure_commission": 0.0,
        "tx_count": 0,
        "by_item_group": {},
    }

    out = []
    rules_map = {r["profile"]: _get_profile_rules(r["profile"]) for r in receivers}
    performer_pct_cache = {}

    def add_totals(item_group, amount, is_invoice):
        totals["total_commission"] += amount
        totals["tx_count"] += 1
        if is_invoice:
            totals["invoice_commission"] += amount
        else:
            totals["procedure_commission"] += amount
        totals["by_item_group"][item_group] = totals["by_item_group"].get(item_group, 0) + amount

    for rec in receivers:
        rules = rules_map.get(rec["profile"]) or []
        receiver_type = rec["receiver_type"]
        receiver_prac = rec.get("receiver_practitioner")

        # Invoice details
        for line in invoice_lines:
            ig = _normalize_str(line.get("item_group"))
            if not ig:
                continue

            inv_so = _normalize_str(line.get("effective_source_order")) or "OPD"

            applicable = []
            for rule in rules:
                if _normalize_str(rule.get("rule_type")) not in ("Invoice Percent", "Invoice Fixed"):
                    continue
                if _normalize_str(rule.get("item_group")) != ig:
                    continue
                if _rule_has_source_order() and not _matches_source_order(rule.get("source_order"), inv_so):
                    continue
                if _match_invoice_rule(rule, receiver_type, receiver_prac, line.get("requesting_practitioner")):
                    applicable.append(rule)

            final_rules = _pick_rules(applicable)
            if not final_rules:
                continue

            base = flt(line.get("base_amount"))
            sign = _commission_sign(base, line.get("is_return"))
            for rule in final_rules:
                c = _calc_commission(rule, base, sign=sign)
                if not c:
                    continue

                scenario = _normalize_str(rule.get("applies_when") or "Any") or "Any"

                out.append({
                    "date": line.get("posting_date"),
                    "reference_type": "Sales Invoice",
                    "reference": line.get("invoice"),
                    "item_or_procedure": f"{line.get('item_code') or ''} - {line.get('item_name') or ''}".strip(" -"),
                    "item_group": ig,
                    "scenario": scenario,
                    "source_order": inv_so,

                    "requesting_practitioner": line.get("requesting_practitioner"),
                    "performing_practitioner": None,
                    "anesthesia_practitioner": None,

                    "base_amount": base,
                    "rule": (
                        f"{rule.get('rule_type')} | {scenario} | {rule.get('percent') or rule.get('amount')}"
                        + (f" | SO:{rule.get('source_order')}" if _rule_has_source_order() and _normalize_str(rule.get("source_order")) else "")
                    ),
                    "commission_amount": c,
                })
                add_totals(ig, c, is_invoice=True)

        # Procedure details (net requester % when requester/requester-only)
        for cp in cps:
            ig = _get_procedure_item_group(cp)
            eff_so = _normalize_str(cp.get("__effective_source_order_cached")) or "OPD"
            inv = cp.get("__invoice_cached")

            applicable = []
            for rule in rules:
                if _normalize_str(rule.get("rule_type")) != "Procedure Percent":
                    continue
                if _normalize_str(rule.get("item_group")) != ig:
                    continue
                if _rule_has_source_order() and not _matches_source_order(rule.get("source_order"), eff_so):
                    continue

                if _match_procedure_rule(
                    rule,
                    receiver_type,
                    receiver_prac,
                    cp.get("requesting_practitioner"),
                    cp.get("performing_practitioner"),
                    cp.get("anesthesia_practitioner"),  # UPDATED
                ):
                    applicable.append(rule)

            final_rules = _pick_rules(applicable)
            if not final_rules:
                continue

            base = flt(cp.get("__base_amount_cached") or 0)
            requester_pct = _sum_rule_percent(final_rules)

            subtract_set = {"Requester", "Requester Only"}
            scenario_hit = any(_normalize_str(r.get("applies_when")) in subtract_set for r in final_rules)

            net_pct = requester_pct
            perf_pct = 0.0

            if (
                receiver_type == "Practitioner"
                and _normalize_str(cp.get("requesting_practitioner")) == _normalize_str(receiver_prac)
                and _normalize_str(cp.get("performing_practitioner"))
                and _normalize_str(cp.get("performing_practitioner")) != _normalize_str(receiver_prac)
                and scenario_hit
            ):
                perf_pct = _get_performer_percent_for_cp(cp, ig, eff_so, performer_pct_cache)
                net_pct = max(0.0, requester_pct - perf_pct)

            c = flt(base) * flt(net_pct) / 100.0
            if not c:
                continue

            scenario = _normalize_str(final_rules[0].get("applies_when") or "Any") or "Any"

            out.append({
                "date": cp.get("done_date"),
                "reference_type": "Clinical Procedure",
                "reference": cp.get("name"),
                "item_or_procedure": cp.get("procedure_template") or cp.get("name"),
                "item_group": ig,
                "scenario": scenario,
                "source_order": eff_so,

                "requesting_practitioner": cp.get("requesting_practitioner"),
                "performing_practitioner": cp.get("performing_practitioner"),
                "anesthesia_practitioner": cp.get("anesthesia_practitioner"),  # UPDATED

                "base_amount": base,
                "rule": (
                    f"Procedure Percent | {scenario} | {requester_pct}%"
                    + (f" - Performer({perf_pct}%)" if perf_pct else "")
                    + f" => Net({net_pct}%) | INV:{inv or '-'}"
                    + (f" | SO:{final_rules[0].get('source_order')}" if _rule_has_source_order() and _normalize_str(final_rules[0].get("source_order")) else "")
                ),
                "commission_amount": c,
            })
            add_totals(ig, c, is_invoice=False)

    out.sort(key=lambda d: (_as_date(d.get("date")), d.get("reference") or ""))
    return out, totals


# ============================================================
# Build Item Group summary (Scenario split + Source Order split)
# ============================================================
def build_item_group_summary(receivers, invoice_lines, cps, filters):
    totals = {
        "total_commission": 0.0,
        "invoice_commission": 0.0,
        "procedure_commission": 0.0,
        "tx_count": 0,
        "by_item_group": {},
    }

    rules_map = {r["profile"]: _get_profile_rules(r["profile"]) for r in receivers}

    agg = {}
    performer_pct_cache = {}

    def add_bucket(item_group, scenario, source_order, base, commission, is_invoice):
        scenario = scenario or "Any"
        source_order = source_order or "OPD"

        key = (item_group, scenario, source_order)
        row = agg.setdefault(key, {
            "item_group": item_group,
            "scenario": scenario,
            "source_order": source_order,
            "base_total": 0.0,
            "commission_total": 0.0,
            "effective_pct": 0.0,
        })

        row["base_total"] += flt(base)
        row["commission_total"] += flt(commission)

        totals["total_commission"] += flt(commission)
        totals["tx_count"] += 1
        totals["by_item_group"][item_group] = totals["by_item_group"].get(item_group, 0) + flt(commission)

        if is_invoice:
            totals["invoice_commission"] += flt(commission)
        else:
            totals["procedure_commission"] += flt(commission)

    # Invoice buckets
    for rec in receivers:
        rules = rules_map.get(rec["profile"]) or []
        receiver_type = rec["receiver_type"]
        receiver_prac = rec.get("receiver_practitioner")

        for line in invoice_lines:
            ig = _normalize_str(line.get("item_group"))
            if not ig:
                continue

            inv_so = _normalize_str(line.get("effective_source_order")) or "OPD"

            applicable = []
            for rule in rules:
                if _normalize_str(rule.get("rule_type")) not in ("Invoice Percent", "Invoice Fixed"):
                    continue
                if _normalize_str(rule.get("item_group")) != ig:
                    continue
                if _rule_has_source_order() and not _matches_source_order(rule.get("source_order"), inv_so):
                    continue
                if _match_invoice_rule(rule, receiver_type, receiver_prac, line.get("requesting_practitioner")):
                    applicable.append(rule)

            final_rules = _pick_rules(applicable)
            if not final_rules:
                continue

            # base = flt(line.get("base_amount"))
            # for rule in final_rules:
            #     commission = _calc_commission(rule, base)
            #     if not commission:
            #         continue
            base = flt(line.get("base_amount"))
            sign = _commission_sign(base, line.get("is_return"))
            for rule in final_rules:
                commission = _calc_commission(rule, base, sign=sign)
                if not commission:
                    continue
                scenario = _normalize_str(rule.get("applies_when") or "Any") or "Any"
                add_bucket(ig, scenario, inv_so, base, commission, is_invoice=True)

    # Procedure buckets (net requester % when requester/requester-only)
    for rec in receivers:
        rules = rules_map.get(rec["profile"]) or []
        receiver_type = rec["receiver_type"]
        receiver_prac = rec.get("receiver_practitioner")

        for cp in cps:
            ig = _get_procedure_item_group(cp)
            eff_so = _normalize_str(cp.get("__effective_source_order_cached")) or "OPD"

            applicable = []
            for rule in rules:
                if _normalize_str(rule.get("rule_type")) != "Procedure Percent":
                    continue
                if _normalize_str(rule.get("item_group")) != ig:
                    continue
                if _rule_has_source_order() and not _matches_source_order(rule.get("source_order"), eff_so):
                    continue

                if _match_procedure_rule(
                    rule,
                    receiver_type,
                    receiver_prac,
                    cp.get("requesting_practitioner"),
                    cp.get("performing_practitioner"),
                    cp.get("anesthesia_practitioner"),  # UPDATED
                ):
                    applicable.append(rule)

            final_rules = _pick_rules(applicable)
            if not final_rules:
                continue

            base = flt(cp.get("__base_amount_cached") or 0)
            requester_pct = _sum_rule_percent(final_rules)

            subtract_set = {"Requester", "Requester Only"}
            scenario = _normalize_str(final_rules[0].get("applies_when") or "Any") or "Any"
            scenario_hit = any(_normalize_str(r.get("applies_when")) in subtract_set for r in final_rules)

            net_pct = requester_pct
            if (
                receiver_type == "Practitioner"
                and _normalize_str(cp.get("requesting_practitioner")) == _normalize_str(receiver_prac)
                and _normalize_str(cp.get("performing_practitioner"))
                and _normalize_str(cp.get("performing_practitioner")) != _normalize_str(receiver_prac)
                and scenario_hit
            ):
                perf_pct = _get_performer_percent_for_cp(cp, ig, eff_so, performer_pct_cache)
                net_pct = max(0.0, requester_pct - perf_pct)

            commission = flt(base) * flt(net_pct) / 100.0
            if not commission:
                continue

            add_bucket(ig, scenario, eff_so, base, commission, is_invoice=False)

    rows = []
    for (_ig, _sc, _so), r in agg.items():
        if flt(r["base_total"]) > 0:
            r["effective_pct"] = (flt(r["commission_total"]) / flt(r["base_total"])) * 100.0
        else:
            r["effective_pct"] = 0.0
        rows.append(r)

    rows.sort(key=lambda d: (flt(d.get("commission_total")), flt(d.get("base_total"))), reverse=True)
    return rows, totals


# ============================================================
# KPI cards + chart
# ============================================================
def make_kpis_and_chart(totals, details_mode: bool):
    summary = [
        {"label": _("Total Commission"), "value": flt(totals.get("total_commission")), "datatype": "Currency"},
        {"label": _("Invoice Commission"), "value": flt(totals.get("invoice_commission")), "datatype": "Currency"},
        {"label": _("Procedure Commission"), "value": flt(totals.get("procedure_commission")), "datatype": "Currency"},
        {"label": _("Transactions"), "value": int(totals.get("tx_count") or 0), "datatype": "Int"},
    ]

    if details_mode:
        by = totals.get("by_item_group") or {}
        labels = list(by.keys())
        values = [flt(by[k]) for k in labels]
        chart = {
            "data": {"labels": labels, "datasets": [{"name": _("Commission"), "values": values}]},
            "type": "bar",
        }
        return summary, chart

    return summary, None
