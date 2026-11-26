import frappe


def _request_cache(attr_name):
    if getattr(getattr(frappe, "flags", None), "in_test", False):
        return None
    local = getattr(frappe, "local", None)
    if local is None:
        return None
    cache = getattr(local, attr_name, None)
    if not isinstance(cache, dict):
        cache = {}
        setattr(local, attr_name, cache)
    return cache


def clear_access_cache():
    local = getattr(frappe, "local", None)
    if local is None:
        return
    for attr_name in ("_dm_doctype_exists_cache", "_dm_user_departments_cache"):
        if hasattr(local, attr_name):
            setattr(local, attr_name, {})


def doctype_exists(doctype):
    cache = _request_cache("_dm_doctype_exists_cache")
    if cache is not None and doctype in cache:
        return cache[doctype]
    exists = bool(frappe.db.exists("DocType", doctype))
    if cache is not None:
        cache[doctype] = exists
    return exists


def department_doctype_exists():
    return doctype_exists("Department")


def employee_doctype_exists():
    return doctype_exists("Employee")


def department_access_enabled():
    return department_doctype_exists() and employee_doctype_exists()


@frappe.whitelist()
def get_document_access_capabilities():
    has_department = department_doctype_exists()
    has_employee = employee_doctype_exists()
    return {
        "department": has_department,
        "employee": has_employee,
        "department_permissions": has_department and has_employee,
    }


def get_user_departments(user):
    if not user or user == "Guest" or not department_access_enabled():
        return set()
    cache = _request_cache("_dm_user_departments_cache")
    if cache is not None and user in cache:
        return set(cache[user])
    departments = {
        department
        for department in frappe.get_all(
            "Employee",
            filters={"user_id": user, "status": "Active"},
            pluck="department",
            limit_page_length=0,
        )
        if department
    }
    if cache is not None:
        cache[user] = frozenset(departments)
    return departments


def matches_access_rules(doc, user_roles, user_departments):
    allowed_roles = {
        row.role for row in (doc.get("roles_with_access") or []) if row.role
    }
    allowed_departments = set()
    if department_access_enabled():
        allowed_departments = {
            row.department
            for row in (doc.get("departments_with_access") or [])
            if row.department
        }
    has_restrictions = bool(allowed_roles or allowed_departments)
    allowed = bool(
        allowed_roles.intersection(user_roles)
        or allowed_departments.intersection(user_departments)
    )
    return has_restrictions, allowed


def sql_access_parts(table_alias, parenttype, user, roles_sql):
    escaped_parenttype = frappe.db.escape(parenttype)
    escaped_user = frappe.db.escape(user)
    role_rows = (
        "EXISTS ("
        "SELECT 1 FROM `tabDocument Role Access` access_role "
        f"WHERE access_role.parent = {table_alias}.name "
        f"AND access_role.parenttype = {escaped_parenttype}"
        ")"
    )
    role_match = (
        "EXISTS ("
        "SELECT 1 FROM `tabDocument Role Access` access_role "
        f"WHERE access_role.parent = {table_alias}.name "
        f"AND access_role.parenttype = {escaped_parenttype} "
        f"AND access_role.role IN ({roles_sql})"
        ")"
    )
    if not department_access_enabled():
        return {
            "has_restrictions": role_rows,
            "matches": role_match,
        }

    department_rows = (
        "EXISTS ("
        "SELECT 1 FROM `tabDocument Department Access` access_department "
        f"WHERE access_department.parent = {table_alias}.name "
        f"AND access_department.parenttype = {escaped_parenttype}"
        ")"
    )
    department_match = (
        "EXISTS ("
        "SELECT 1 FROM `tabDocument Department Access` access_department "
        "INNER JOIN `tabEmployee` access_employee "
        "ON access_employee.department = access_department.department "
        f"WHERE access_department.parent = {table_alias}.name "
        f"AND access_department.parenttype = {escaped_parenttype} "
        f"AND access_employee.user_id = {escaped_user} "
        "AND access_employee.status = 'Active'"
        ")"
    )
    return {
        "has_restrictions": f"({role_rows} OR {department_rows})",
        "matches": f"({role_match} OR {department_match})",
    }
