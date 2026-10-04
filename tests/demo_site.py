"""Tiny local web app used by the tests and for manual demos.

    uv run python tests/demo_site.py --port 8002 [--variant original|renamed|ambiguous|removed|filterbug]

Variants change only the login button, to exercise locator healing:
  original  - <button data-testid="login-submit">Log in</button>
  renamed   - testid and text changed ("Sign in"): a confident, unambiguous heal
  ambiguous - two identical "Sign in" buttons: healing must refuse (ambiguous)
  removed   - no submit button at all: healing must refuse (no confident candidate)
  moved     - the same button, 40px further right inside the form (layout tracking)
  relocated - the same button in the page header, outside the form it submits (layout tracking)
/orders is a list of plain <div> cards with status filter chips:
  filterbug - the Cancelled and Returned chips both keep every order that is not delivered
              (status !== 'delivered'): the chip highlights and the count updates, but the list is wrong
The variant can also be switched at runtime: GET /__variant?name=renamed"""
from __future__ import annotations

import argparse
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

_BUTTONS = {
    "original": '<button type="submit" data-testid="login-submit">Log in</button>',
    "renamed": '<button type="submit" data-testid="signin-btn">Sign in</button>',
    "ambiguous": '<button type="submit">Sign in</button> <button type="submit">Sign in</button>',
    "removed": '<button type="button" onclick="history.back()">Cancel</button>',
    "moved": '<button type="submit" data-testid="login-submit" style="margin-left:40px">Log in</button>',
    "relocated": "",  # rendered in the header, see _HEADER_BUTTON
}
_HEADER_BUTTON = '<button type="submit" form="login-form" data-testid="login-submit">Log in</button>'

LOGIN = """<!doctype html><html><head><title>Sign in</title></head><body>
<header><nav aria-label="Top"><a href="/">Home</a> <button type="button">Help</button></nav>{header_button}</header>
<main>
  <h1>Sign in</h1>
  <form id="login-form" onsubmit="event.preventDefault(); if (document.getElementById('password').value) {
      location.href = '/dashboard?session=s3cr3t-token&tab=overview'; } else {
      document.getElementById('error').textContent = 'Password is required'; }">
    <label for="email">Email</label> <input id="email" name="email" type="email">
    <label for="password">Password</label> <input id="password" name="password" type="password">
    {button}
    <p id="error" role="alert"></p>
  </form>
</main></body></html>"""

DASHBOARD = """<!doctype html><html><head><title>Dashboard</title></head><body>
<main><h1>Welcome back</h1><p>You have 3 new messages.</p><a href="/login">Log out</a></main></body></html>"""

SETTINGS = """<!doctype html><html><head><title>Settings</title></head><body><main>
<h1>Settings</h1>
<label><input type="checkbox" name="remember"> Remember me</label>
<label for="theme">Theme</label>
<select id="theme"><option value="light">Light</option><option value="dark">Dark</option></select>
<label for="nick">Nickname</label> <input id="nick" placeholder="Nickname">
<button type="button" onclick="document.getElementById('status').textContent =
  'Saved ' + document.getElementById('theme').value + ' for ' + document.getElementById('nick').value">Save</button>
<p id="status"></p>
</main></body></html>"""

HOME = """<!doctype html><html><head><title>Demo shop</title></head><body><main>
<h1>Demo shop</h1><a href="/login">Sign in</a> <a href="/settings">Settings</a></main></body></html>"""


# -- a small OrangeHRM-like admin app (for agent-mode tests) -----------------------------------
# /hrm/login prints its demo credentials; the users table uses ARIA rows with icon-only edit/delete
# buttons; data lives in the browser (fresh per isolated test context). Variant "renamed" changes the
# nav label and the edit icon, to exercise replay deviation.

_HRM_JS = """
const SEED = [{id: 1, username: 'Admin', role: 'Admin', name: 'Paul Collings', status: 'Enabled'},
              {id: 2, username: 'fiona.g', role: 'ESS', name: 'Fiona Grace', status: 'Enabled'},
              {id: 3, username: 'linda.a', role: 'ESS', name: 'Linda Anderson', status: 'Disabled'}];
const users = () => JSON.parse(localStorage.getItem('hrm_users') || JSON.stringify(SEED));
const saveUsers = (u) => localStorage.setItem('hrm_users', JSON.stringify(u));
function guard() { if (!sessionStorage.getItem('hrm_user')) location.href = '/hrm/login'; }
"""

_HRM_NAV = """<nav aria-label="Main"><ul>
<li><a href="/hrm/admin">{admin}</a></li><li><a href="/hrm/pim">PIM</a></li><li><a href="/hrm/leave">Leave</a></li>
<li><a href="/hrm/dashboard">Dashboard</a></li></ul></nav>"""

HRM_LOGIN = """<!doctype html><html><head><title>HRM</title></head><body><main>
<h5>Login</h5>
<div class="demo-credentials"><p>Username : Admin</p><p>Password : admin123</p></div>
<form onsubmit="event.preventDefault(); const u = document.getElementById('u').value, p = document.getElementById('p').value;
  if (u === 'Admin' && p === 'admin123') {  /* like OrangeHRM: validate in the background, then navigate */
    fetch('/hrm/validate').then(() => { sessionStorage.setItem('hrm_user', u); location.href = '/hrm/dashboard'; }); }
  else { document.getElementById('err').textContent = 'Invalid credentials'; }">
  <input id="u" name="username" placeholder="Username"> <input id="p" name="password" type="password" placeholder="Password">
  <button type="submit">Login</button><p id="err" role="alert"></p>
</form></main></body></html>"""

HRM_DASHBOARD = """<!doctype html><html><head><title>HRM</title><script>{js} guard();</script></head><body>
{nav}<main><h6>Dashboard</h6><p>Time at Work</p></main></body></html>"""

HRM_ADMIN = """<!doctype html><html><head><title>HRM</title><script>{js} guard();</script></head><body>
{nav}<main><h5>System Users</h5><p id="toast" role="status"></p>
<div role="table" aria-label="Users"><div role="rowgroup"><div role="row">
  <span role="columnheader"></span><span role="columnheader">Username</span><span role="columnheader">User Role</span>
  <span role="columnheader">Employee Name</span><span role="columnheader">Status</span><span role="columnheader">Actions</span>
</div></div><div role="rowgroup" id="rows"></div></div>
<script>
const flash = sessionStorage.getItem('hrm_flash');
if (flash) { document.getElementById('toast').textContent = flash; sessionStorage.removeItem('hrm_flash'); }
document.getElementById('rows').innerHTML = users().map(u => `<div role="row">
  <span role="cell"><input type="checkbox" aria-label="Select"></span><span role="cell">${u.username}</span>
  <span role="cell">${u.role}</span><span role="cell">${u.name}</span><span role="cell">${u.status}</span>
  <span role="cell"><button type="button" class="oxd-icon-button" onclick="alert('Delete disabled in demo')"><i class="oxd-icon bi-trash"></i></button>
  <button type="button" class="oxd-icon-button" onclick="location.href='/hrm/admin/edit?id=${u.id}'"><i class="oxd-icon {edit_icon}"></i></button></span>
</div>`).join('');
</script></main></body></html>"""

HRM_EDIT = """<!doctype html><html><head><title>HRM</title><script>{js} guard();</script></head><body>
{nav}<main><h6>Edit User</h6>
<form id="f" onsubmit="event.preventDefault(); const id = +new URLSearchParams(location.search).get('id');
  const all = users(); const u = all.find(x => x.id === id);
  u.username = document.getElementById('username').value; u.name = document.getElementById('name').value;
  u.status = document.getElementById('status').value; saveUsers(all);
  sessionStorage.setItem('hrm_flash', 'Successfully Updated'); location.href = '/hrm/admin';">
  <label for="role">User Role</label><select id="role"><option>Admin</option><option>ESS</option></select>
  <label for="name">Employee Name</label><input id="name">
  <label for="status">Status</label><select id="status"><option>Enabled</option><option>Disabled</option></select>
  <label for="username">Username</label><input id="username">
  <button type="button" onclick="location.href='/hrm/admin'">Cancel</button> <button type="submit">Save</button>
</form>
<script>
const u = users().find(x => x.id === +new URLSearchParams(location.search).get('id'));
document.getElementById('role').value = u.role; document.getElementById('name').value = u.name;
document.getElementById('status').value = u.status; document.getElementById('username').value = u.username;
</script></main></body></html>"""


# -- orders with status filter chips (for result-checking tests) --------------------------------
# 8 orders as plain <div> cards (no list markup): 4 delivered, 3 cancelled ($310), 1 returned.
# A server clock changes by itself, like any live value on a page.
ORDERS = """<!doctype html><html><head><title>Orders</title></head><body><main>
<h5>Orders</h5>
<p>Server time <span id="clock">12:00:00</span></p>
<div class="tiles"><span>Orders</span> <b id="n"></b> <span>Total</span> <b id="sum"></b></div>
<div role="group" aria-label="Filter orders by status">
  <button type="button" class="chip" data-f="all">All</button>
  <button type="button" class="chip" data-f="delivered">Delivered</button>
  <button type="button" class="chip" data-f="cancelled">Cancelled</button>
  <button type="button" class="chip" data-f="returned">Returned</button>
</div>
<p id="count"></p>
<div class="orders" id="list"></div>
<script>
const BUG = {bug};
const O = [
  {id: 1001, status: 'delivered', usd: 40}, {id: 1002, status: 'delivered', usd: 25},
  {id: 1003, status: 'cancelled', usd: 90}, {id: 1004, status: 'delivered', usd: 60},
  {id: 1005, status: 'returned', usd: 55}, {id: 1006, status: 'cancelled', usd: 120},
  {id: 1007, status: 'delivered', usd: 15}, {id: 1008, status: 'cancelled', usd: 100}];
const LABEL = {delivered: 'Delivered', cancelled: 'Cancelled', returned: 'Returned (refund issued)'};
function keep(f, o) {
  if (f === 'all') return true;
  if (BUG && f !== 'delivered') return o.status !== 'delivered';  // the planted bug
  return o.status === f;
}
function render(f) {
  const rows = O.filter((o) => keep(f, o));
  document.getElementById('list').innerHTML = rows.map((o) => `<div class="order-card">
    <span class="title">Order #${o.id}</span> <span class="badge">${LABEL[o.status]}</span> <span>$${o.usd}</span></div>`).join('');
  document.getElementById('count').textContent = `Showing ${rows.length} of ${O.length} orders`;
  document.getElementById('n').textContent = rows.length;
  document.getElementById('sum').textContent = '$' + rows.reduce((a, o) => a + o.usd, 0);
  document.querySelectorAll('.chip').forEach((c) => c.setAttribute('aria-pressed', String(c.dataset.f === f)));
}
document.querySelectorAll('.chip').forEach((c) => c.addEventListener('click', () => render(c.dataset.f)));
render('all');
let t = 0;
setInterval(() => { t += 1; document.getElementById('clock').textContent = '12:00:' + String(t % 60).padStart(2, '0'); }, 150);
</script></main></body></html>"""


def _hrm_page(path: str, variant: str) -> str | None:
    renamed = variant == "renamed"
    fill = {"{js}": _HRM_JS, "{nav}": _HRM_NAV.replace("{admin}", "Administration" if renamed else "Admin"),
            "{edit_icon}": "bi-pencil-square" if renamed else "bi-pencil-fill"}
    template = {"/hrm/login": HRM_LOGIN, "/hrm/dashboard": HRM_DASHBOARD, "/hrm/admin": HRM_ADMIN,
                "/hrm/admin/edit": HRM_EDIT}.get(path)
    if template is None:
        return None
    for key, value in fill.items():
        template = template.replace(key, value)
    return template


class DemoSite:
    def __init__(self, port: int = 0, variant: str = "original"):
        self.variant = variant
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parts = urlsplit(self.path)
                if parts.path == "/hrm/validate":
                    time.sleep(0.4)  # a slow login request: the page navigates only after it returns
                    return self._send(200, "{\"ok\": true}")
                if parts.path == "/__variant":
                    site.variant = parse_qs(parts.query).get("name", ["original"])[0]
                    return self._send(200, f"variant={site.variant}")
                path = parts.path.rstrip("/") or "/"
                page = _hrm_page(path, site.variant) or {
                    "/": HOME, "/login": LOGIN.replace("{button}", _BUTTONS.get(site.variant, _BUTTONS["original"]))
                    .replace("{header_button}", _HEADER_BUTTON if site.variant == "relocated" else ""),
                    "/dashboard": DASHBOARD, "/settings": SETTINGS,
                    "/orders": ORDERS.replace("{bug}", "true" if site.variant == "filterbug" else "false"),
                }.get(path)
                if page is None:
                    return self._send(404, "<!doctype html><title>Not found</title><h1>Not found</h1>")
                self._send(200, page)

            def _send(self, code: int, body: str) -> None:
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}/"

    def start(self) -> "DemoSite":
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8002)
    parser.add_argument("--variant", default="original", choices=sorted(_BUTTONS) + ["filterbug"])
    args = parser.parse_args()
    site = DemoSite(args.port, args.variant)
    print(f"Demo site on {site.base_url} (variant: {site.variant}); switch with {site.base_url}__variant?name=renamed")
    site.server.serve_forever()
