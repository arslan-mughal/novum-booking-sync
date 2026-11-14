"""In-memory fake of the parts of Microsoft identity + Graph/OneDrive that the sync uses.
Good enough to test OUR client logic; it is not a claim about how the real service behaves in every corner."""
import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse, parse_qs


class FakeCloud:
    def __init__(self):
        self.lock = threading.RLock()
        self.files = {}          # path -> dict(data, etag, id)
        self.folders = {""}      # set of folder paths
        self.ids = {}            # id -> path
        self.valid_access = set()
        self.valid_refresh = {"RT-0"}
        self.app_secret = "sek"
        self.expires_in = 3600
        self.auto_create_parents = True
        self.throttle = 0        # next N graph requests answer 429
        self.locked = set()      # paths that answer 423
        self.sessions = {}
        self.requests = []       # (method, path) log
        self.device_polls = 0
        self.rt_counter = 0
        self.invalidate_old_rt = False

    # helpers
    def put(self, path, data):
        with self.lock:
            self._mkparents(path)
            etag = '"%s"' % uuid.uuid4().hex[:8]
            fid = self.files.get(path, {}).get("id") or "id-" + uuid.uuid4().hex[:6]
            self.files[path] = {"data": data, "etag": etag, "id": fid}
            self.ids[fid] = path
            return self.item(path)

    def _mkparents(self, path):
        parts = path.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            self.folders.add("/".join(parts[:i]))

    def parent_exists(self, path):
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        return parent in self.folders

    def item(self, path):
        f = self.files[path]
        return {"id": f["id"], "name": path.rsplit("/", 1)[-1], "eTag": f["etag"], "size": len(f["data"]), "file": {}}

    def bump_etag(self, path):          # simulate somebody else editing the file
        with self.lock:
            self.files[path]["etag"] = '"%s"' % uuid.uuid4().hex[:8]


class Handler(BaseHTTPRequestHandler):
    cloud: FakeCloud = None
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # ---- plumbing
    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _send(self, code, obj=None, raw=None, headers=None):
        body = raw if raw is not None else (json.dumps(obj).encode() if obj is not None else b"")
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", "application/json" if raw is None else "application/octet-stream")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, ecode, msg="x"):
        self._send(code, {"error": {"code": ecode, "message": msg}})

    def do_GET(self): self.route("GET")
    def do_POST(self): self.route("POST")
    def do_PUT(self): self.route("PUT")

    # ---- routing
    def route(self, method):
        c = self.cloud
        u = urlparse(self.path)
        path, q = unquote(u.path), parse_qs(u.query)
        body = self._body()
        if path.endswith("/oauth2/v2.0/token"):
            return self.token(parse_qs(body.decode()))
        if path.endswith("/oauth2/v2.0/devicecode"):
            return self._send(200, {"device_code": "DC", "user_code": "ABCD-1234", "interval": 0, "expires_in": 60,
                                    "verification_uri": "https://microsoft.com/devicelogin", "message": "go sign in"})
        if path.startswith("/upload/"):
            return self.upload_chunk(method, path, body)
        # Graph from here
        with c.lock:
            c.requests.append((method, path))
            if c.throttle > 0:
                c.throttle -= 1
                return self._send(429, {"error": {"code": "activityLimitReached", "message": "slow"}},
                                  headers={"Retry-After": "0"})
        tok = (self.headers.get("Authorization") or "").replace("Bearer ", "")
        if tok not in c.valid_access:
            return self._err(401, "InvalidAuthenticationToken", "expired")
        m = re.match(r"^/v1\.0/(?:me/drive|users/[^/]+/drive|drives/[^/]+)(.*)$", path)
        if not m:
            return self._err(404, "notFound")
        rest = m.group(1)
        if rest == "/root/children" and method == "POST":
            return self.create_folder("", json.loads(body))
        mm = re.match(r"^/items/([^/]+)/content$", rest)
        if mm and method == "GET":
            p = c.ids.get(mm.group(1))
            return self._send(200, raw=c.files[p]["data"]) if p else self._err(404, "itemNotFound")
        mm = re.match(r"^/root:/(.*?):(/content|/children|/createUploadSession)?$", rest)
        if not mm:
            return self._err(404, "notFound", rest)
        ipath, tail = mm.group(1), mm.group(2) or ""
        if tail == "" and method == "GET":
            if ipath in c.files:
                return self._send(200, c.item(ipath))
            if ipath in c.folders:
                return self._send(200, {"id": "f-" + ipath, "name": ipath.split("/")[-1], "folder": {}})
            return self._err(404, "itemNotFound")
        if tail == "/content" and method == "GET":
            return self._send(200, raw=c.files[ipath]["data"]) if ipath in c.files else self._err(404, "itemNotFound")
        if tail == "/children" and method == "GET":
            if ipath not in c.folders:
                return self._err(404, "itemNotFound")
            pre = ipath + "/" if ipath else ""
            names = sorted({p[len(pre):].split("/")[0] for p in list(c.files) + list(c.folders)
                            if p.startswith(pre) and p != ipath and p[len(pre):]})
            skip = int((q.get("$skiptoken") or ["0"])[0])
            page, nxt = names[skip:skip + 2], skip + 2          # tiny pages to exercise @odata.nextLink
            out = {"value": [{"name": n} for n in page]}
            if nxt < len(names):
                out["@odata.nextLink"] = f"http://{self.headers['Host']}{u.path}?$select=name&$skiptoken={nxt}"
            return self._send(200, out)
        if tail == "/children" and method == "POST":
            return self.create_folder(ipath, json.loads(body))
        if tail == "/content" and method == "PUT":
            if ipath in c.locked:
                return self._err(423, "resourceLocked")
            if not c.auto_create_parents and not c.parent_exists(ipath):
                return self._err(404, "itemNotFound", "parent")
            if (q.get("@microsoft.graph.conflictBehavior") == ["fail"]) and ipath in c.files:
                return self._err(409, "nameAlreadyExists")
            return self._send(201, c.put(ipath, body))
        if tail == "/createUploadSession" and method == "POST":
            if ipath in c.locked:
                return self._err(423, "resourceLocked")
            im = self.headers.get("If-Match")
            if im and ipath in c.files and c.files[ipath]["etag"] != im:
                return self._err(412, "preconditionFailed")
            if not c.parent_exists(ipath):
                return self._err(404, "itemNotFound", "parent")
            sid = uuid.uuid4().hex
            c.sessions[sid] = {"path": ipath, "buf": bytearray()}
            return self._send(200, {"uploadUrl": f"http://{self.headers['Host']}/upload/{sid}"})
        return self._err(400, "badRequest", f"{method} {rest}")

    def create_folder(self, parent, body):
        c = self.cloud
        full = (parent + "/" if parent else "") + body["name"]
        with c.lock:
            if parent and parent not in c.folders:
                return self._err(404, "itemNotFound")
            if full in c.folders or full in c.files:
                return self._err(409, "nameAlreadyExists")
            c.folders.add(full)
        return self._send(201, {"id": "f-" + full, "name": body["name"], "folder": {}})

    def upload_chunk(self, method, path, body):
        c = self.cloud
        s = c.sessions.get(path.rsplit("/", 1)[-1])
        if not s:
            return self._err(404, "itemNotFound")
        m = re.match(r"bytes (\d+)-(\d+)/(\d+)", self.headers.get("Content-Range", ""))
        a, b, total = (int(x) for x in m.groups())
        assert a == len(s["buf"]), "chunks must arrive in order"
        s["buf"] += body
        if b + 1 < total:
            return self._send(202, {"nextExpectedRanges": [f"{b + 1}-"]})
        return self._send(201, c.put(s["path"], bytes(s["buf"])))

    def token(self, f):
        c = self.cloud
        g = {k: v[0] for k, v in f.items()}
        grant = g.get("grant_type")
        if grant == "client_credentials":
            if g.get("client_secret") != c.app_secret:
                return self._send(401, {"error": "invalid_client", "error_description": "AADSTS7000215 bad secret"})
            tok = "AT-app-" + uuid.uuid4().hex[:6]
            c.valid_access.add(tok)
            return self._send(200, {"access_token": tok, "expires_in": c.expires_in, "token_type": "Bearer"})
        if grant == "refresh_token":
            if g["refresh_token"] not in c.valid_refresh:
                return self._send(400, {"error": "invalid_grant", "error_description": "AADSTS70000 token expired"})
            c.rt_counter += 1
            new_rt = f"RT-{c.rt_counter}"
            if c.invalidate_old_rt:
                c.valid_refresh.discard(g["refresh_token"])
            c.valid_refresh.add(new_rt)
            tok = "AT-usr-" + uuid.uuid4().hex[:6]
            c.valid_access.add(tok)
            return self._send(200, {"access_token": tok, "refresh_token": new_rt, "expires_in": c.expires_in})
        if grant == "urn:ietf:params:oauth:grant-type:device_code":
            c.device_polls += 1
            if c.device_polls < 2:
                return self._send(400, {"error": "authorization_pending"})
            c.valid_refresh.add("RT-device")
            tok = "AT-dev"
            c.valid_access.add(tok)
            return self._send(200, {"access_token": tok, "refresh_token": "RT-device", "expires_in": 3600})
        return self._send(400, {"error": "unsupported_grant_type"})


def start(cloud: FakeCloud):
    Handler.cloud = cloud
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"
