frappe.query_reports["Commission Management"] = {
  onload: function (report) {
    const roles = frappe.user_roles || [];
    const privileged_roles = ["System Manager", "Accounts Manager", "Auditor"];
    const is_privileged = privileged_roles.some(role => roles.includes(role));
    const should_force_doctor_scope = roles.includes("Doctor") && !is_privileged;

    const min_doctor_from_date = "2026-03-01";
    const today = frappe.datetime.get_today();

    // Default values on first load
    if (should_force_doctor_scope) {
      report.set_filter_value("from_date", min_doctor_from_date);
    } else {
      report.set_filter_value("from_date", today);
    }
    report.set_filter_value("to_date", today);

    // Doctor-side UI restriction
    if (should_force_doctor_scope) {
      frappe.call({
        method: "frappe.client.get_value",
        args: {
          doctype: "Healthcare Practitioner",
          filters: {
            user_id: frappe.session.user,
          },
          fieldname: ["name"],
        },
        callback: function (r) {
          if (r.message && r.message.name) {
            report.set_filter_value("receiver_practitioner", r.message.name);

            const rf = report.get_filter("receiver_practitioner");
            if (rf) {
              rf.df.hidden = 0;
              rf.refresh();
              $(rf.wrapper).hide();
            }
          }
        },
      });
    }

    const from_filter = report.get_filter("from_date");
    const to_filter = report.get_filter("to_date");

    if (from_filter) {
      from_filter.df.onchange = function () {
        const from_date = report.get_filter_value("from_date");

        if (should_force_doctor_scope && from_date && from_date < min_doctor_from_date) {
          frappe.msgprint(__("From Date cannot be earlier than {0}", [min_doctor_from_date]));
          report.set_filter_value("from_date", min_doctor_from_date);
        }

        const current_from = report.get_filter_value("from_date");
        const to_date = report.get_filter_value("to_date");

        if (to_date && current_from && to_date < current_from) {
          report.set_filter_value("to_date", current_from);
        }
      };
    }

    if (to_filter) {
      to_filter.df.onchange = function () {
        let from_date = report.get_filter_value("from_date");
        let to_date = report.get_filter_value("to_date");

        if (should_force_doctor_scope && from_date && from_date < min_doctor_from_date) {
          report.set_filter_value("from_date", min_doctor_from_date);
          from_date = min_doctor_from_date;
        }

        if (to_date && from_date && to_date < from_date) {
          frappe.msgprint(__("To Date cannot be earlier than From Date."));
          report.set_filter_value("to_date", from_date);
        }
      };
    }
  },

  filters: [
    {
      fieldname: "view",
      label: __("View"),
      fieldtype: "Select",
      options: ["Top Earners", "By Item Group", "Details"],
      default: "Top Earners",
      reqd: 1,
    },
    {
      fieldname: "from_date",
      label: __("From Date"),
      fieldtype: "Date",
      default: frappe.datetime.get_today(),
      reqd: 1,
    },
    {
      fieldname: "to_date",
      label: __("To Date"),
      fieldtype: "Date",
      default: frappe.datetime.get_today(),
      reqd: 1,
    },
    {
      fieldname: "receiver_practitioner",
      label: __("Receiver Practitioner"),
      fieldtype: "Link",
      options: "Healthcare Practitioner",
      get_query: function () {
        const roles = frappe.user_roles || [];
        const privileged_roles = ["System Manager", "Accounts Manager", "Auditor"];
        const is_privileged = privileged_roles.some(role => roles.includes(role));
        const should_force_doctor_scope = roles.includes("Doctor") && !is_privileged;

        if (should_force_doctor_scope) {
          return {
            filters: {
              user_id: frappe.session.user,
            },
          };
        }
      },
    },
    {
      fieldname: "receiver_employee",
      label: __("Receiver Employee"),
      fieldtype: "Link",
      options: "Employee",
      hidden: 1,
    },
    {
      fieldname: "item_group",
      label: __("Item Group"),
      fieldtype: "Link",
      options: "Item Group",
    },
    {
      fieldname: "source_order",
      label: __("Source Order"),
      fieldtype: "Link",
      options: "Source Order",
    },
  ],
};


// frappe.query_reports["Commission Management"] = {
//   filters: [
//     {
//       fieldname: "view",
//       label: __("View"),
//       fieldtype: "Select",
//       options: ["Top Earners", "By Item Group", "Details"],
//       default: "Top Earners",
//       reqd: 1,
//     },
//     {
//       fieldname: "from_date",
//       label: __("From Date"),
//       fieldtype: "Date",
//       default: frappe.datetime.get_today(),
//       reqd: 1,
//     },
//     {
//       fieldname: "to_date",
//       label: __("To Date"),
//       fieldtype: "Date",
//       default: frappe.datetime.get_today(),
//     },

//     // existing
//     {
//       fieldname: "receiver_practitioner",
//       label: __("Receiver Practitioner"),
//       fieldtype: "Link",
//       options: "Healthcare Practitioner",
//     },
//     {
//       fieldname: "receiver_employee",
//       label: __("Receiver Employee"),
//       fieldtype: "Link",
//       options: "Employee",
//       hidden: 1,
//     },

//     // NEW
//     {
//       fieldname: "item_group",
//       label: __("Item Group"),
//       fieldtype: "Link",
//       options: "Item Group",
//     },
//     {
//       fieldname: "source_order",
//       label: __("Source Order"),
//       fieldtype: "Link",
//       options: "Source Order",
//     },
//   ],
// };