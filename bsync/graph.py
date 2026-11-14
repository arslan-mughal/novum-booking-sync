"""Minimal Microsoft Graph / OneDrive client (no SDK, just `requests`).

Two ways to authenticate (pick with environment variables, see README_CLOUD.md):
  * ClientCredentialsAuth  - app-only (work/school accounts; tenant admin must consent). Nothing to renew.
  * RefreshTokenAuth       - acts as one signed-in user (personal OR work/school). Needs a one-time device-code
                             login (login_device.py). The rotating refresh token is written to a token file.

Everything here is covered by tests against an in-memory fake of Graph (tests/mock_graph.py). It has NOT been
run against the real Microsoft service from the build environment.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests

DEFAULT_GRAPH = "https://graph.microsoft.com/v1.0"
DEFAULT_LOGIN = "https://login.microsoftonline.com"
SCOPE_APP = "https://graph.microsoft.com/.default"
SCOPE_DELEGATED_DEFAULT = "https://graph.microsoft.com/Files.ReadWrite offline_access"


def delegated_scope() -> str:
    """Own OneDrive: Files.ReadWrite is enough. For a shared folder / other user's drive set MS_SCOPE to
    'https://graph.microsoft.com/Files.ReadWrite.All offline_access'."""
    return os.environ.get("MS_SCOPE", SCOPE_DELEGATED_DEFAULT)
SIMPLE_UPLOAD_LIMIT = 4 * 1024 * 1024          # below: one PUT; above: resumable upload session
CHUNK = 32 * 320 * 1024                        # upload-session chunks must be multiples of 320 KiB


def graph_base() -> str:
    return os.environ.get("GRAPH_BASE", DEFAULT_GRAPH).rstrip("/")


def login_base() -> str:
    return os.environ.get("LOGIN_BASE", DEFAULT_LOGIN).rstrip("/")


class AuthError(RuntimeError):
    pass


class GraphError(RuntimeError):
    def __init__(self, status: int, message: str, code: str = ""):
        super().__init__(f"HTTP {status} {code}: {message}")
        self.status, self.code = status, code


class NotFound(GraphError):
    pass


class PreconditionFailed(GraphError):
    pass


class Locked(GraphError):
    pass


def enc_path(path: str) -> str:
    """URL-encode each segment of a OneDrive path (umlauts, spaces, '#', '%', '²' ...)."""
    return "/".join(quote(seg, safe="") for seg in path.strip("/").split("/") if seg)


# ---------------------------------------------------------------------------------------------- auth
def _token_request(tenant: str, data: dict) -> dict:
    r = requests.post(f"{login_base()}/{tenant}/oauth2/v2.0/token", data=data, timeout=30)
    try:
        js = r.json()
    except ValueError:
        js = {}
    if r.status_code != 200 or "access_token" not in js:
        raise AuthError(f"{js.get('error', r.status_code)}: {str(js.get('error_description', r.text))[:300]}")
    return js


class _Auth:
    def __init__(self):
        self._tok, self._exp = None, 0.0

    def token(self) -> str:
        if not self._tok or time.time() > self._exp - 90:
            js = self._fetch()
            self._tok = js["access_token"]
            self._exp = time.time() + float(js.get("expires_in", 3600))
        return self._tok

    def invalidate(self):
        self._tok = None

    def _fetch(self) -> dict:
        raise NotImplementedError


class ClientCredentialsAuth(_Auth):
    def __init__(self, tenant: str, client_id: str, client_secret: str):
        super().__init__()
        self.tenant, self.client_id, self.client_secret = tenant, client_id, client_secret

    def _fetch(self) -> dict:
        return _token_request(self.tenant, {"grant_type": "client_credentials", "client_id": self.client_id,
                                            "client_secret": self.client_secret, "scope": SCOPE_APP})


class RefreshTokenAuth(_Auth):
    def __init__(self, tenant: str, client_id: str, refresh_token: str = "", token_file: Optional[str] = None,
                 client_secret: str = ""):
        super().__init__()
        self.tenant, self.client_id, self.client_secret = tenant, client_id, client_secret
        self.token_file = Path(token_file) if token_file else None
        self.env_rt = refresh_token or ""
        self.rt = self.env_rt
        if self.token_file and self.token_file.exists():
            try:
                saved = json.loads(self.token_file.read_text(encoding="utf-8")).get("refresh_token")
                if saved:
                    self.rt = saved                      # the file always holds the newest token
            except (OSError, ValueError):
                pass
        if not self.rt:
            raise AuthError("no refresh token: set MS_REFRESH_TOKEN or run login_device.py first")

    def _persist(self):
        if not self.token_file:
            return
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.token_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"refresh_token": self.rt}), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.token_file)

    def _fetch(self) -> dict:
        errors = []
        for rt in dict.fromkeys([self.rt, self.env_rt]):         # newest first, then the configured one
            if not rt:
                continue
            data = {"grant_type": "refresh_token", "refresh_token": rt, "client_id": self.client_id,
                    "scope": delegated_scope()}
            if self.client_secret:
                data["client_secret"] = self.client_secret
            try:
                js = _token_request(self.tenant, data)
            except AuthError as e:
                errors.append(str(e))
                continue
            new = js.get("refresh_token")
            if new and new != self.rt:
                self.rt = new
                self._persist()
            return js
        raise AuthError("refresh token rejected (" + " | ".join(errors) + "). Run login_device.py again and "
                        "update MS_REFRESH_TOKEN.")


# ---------------------------------------------------------------------------------------------- OneDrive
class OneDrive:
    def __init__(self, auth: _Auth, drive: str = "me", sleep=time.sleep):
        """drive: 'me' | 'user:<upn>' | 'id:<driveId>'"""
        self.auth, self.sleep = auth, sleep
        self.session = requests.Session()
        if drive == "me":
            self.base = "/me/drive"
        elif drive.startswith("user:"):
            self.base = f"/users/{quote(drive[5:], safe='@')}/drive"
        elif drive.startswith("id:"):
            self.base = f"/drives/{quote(drive[3:], safe='')}"
        else:
            raise ValueError(f"bad drive spec {drive!r}")
        self._folders_ok: set = set()

    # -- low level
    def _url(self, path: str, suffix: str = "") -> str:
        return f"{graph_base()}{self.base}/root:/{enc_path(path)}:{suffix}"

    def _req(self, method: str, url: str, *, auth: bool = True, headers: Optional[dict] = None,
             retries: int = 6, expect=(200, 201, 202, 204), **kw) -> requests.Response:
        refreshed = False
        for attempt in range(retries):
            h = dict(headers or {})
            if auth:
                h["Authorization"] = "Bearer " + self.auth.token()
            try:
                r = self.session.request(method, url, headers=h, timeout=60, **kw)
            except requests.RequestException:
                if attempt == retries - 1:
                    raise
                self.sleep(min(2 ** attempt, 30))
                continue
            if r.status_code in expect:
                return r
            if r.status_code == 401 and auth and not refreshed:
                refreshed = True
                self.auth.invalidate()
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                try:
                    wait = float(r.headers.get("Retry-After", ""))
                except ValueError:
                    wait = 2 ** attempt
                self.sleep(min(wait, 120))
                continue
            try:
                err = r.json().get("error", {})
            except ValueError:
                err = {}
            msg, code = err.get("message", r.text[:200]), err.get("code", "")
            cls = {404: NotFound, 412: PreconditionFailed, 423: Locked}.get(r.status_code, GraphError)
            raise cls(r.status_code, msg, code)
        raise GraphError(0, "retries exhausted")

    # -- reading
    def get_item(self, path: str) -> Optional[dict]:
        try:
            return self._req("GET", self._url(path), expect=(200,)).json()
        except NotFound:
            return None

    def download(self, path: str, dest: Path) -> None:
        r = self._req("GET", self._url(path, "/content"), expect=(200,), stream=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(1 << 20):
                fh.write(chunk)

    def read_bytes(self, path: str) -> Optional[bytes]:
        try:
            return self._req("GET", self._url(path, "/content"), expect=(200,)).content
        except NotFound:
            return None

    def list_names(self, path: str) -> list:
        """File/folder names directly inside a folder ([] if the folder does not exist)."""
        url = self._url(path, "/children") + "?$select=name&$top=200"
        names: list = []
        while url:
            try:
                js = self._req("GET", url, expect=(200,)).json()
            except NotFound:
                return []
            names += [i["name"] for i in js.get("value", [])]
            url = js.get("@odata.nextLink")
        return names

    # -- writing
    def ensure_folder(self, path: str) -> None:
        parts = [p for p in path.strip("/").split("/") if p]
        for i in range(1, len(parts) + 1):
            sub = "/".join(parts[:i])
            if sub in self._folders_ok:
                continue
            if self.get_item(sub) is None:
                parent = "/".join(parts[:i - 1])
                url = (self._url(parent, "/children") if parent else f"{graph_base()}{self.base}/root/children")
                body = {"name": parts[i - 1], "folder": {}, "@microsoft.graph.conflictBehavior": "fail"}
                try:
                    self._req("POST", url, json=body, expect=(200, 201))
                except GraphError as e:
                    if e.status != 409:             # 409 = somebody created it meanwhile: fine
                        raise
            self._folders_ok.add(sub)

    def upload_bytes(self, path: str, data: bytes, replace: bool = False) -> str:
        """Upload a (small) file. Returns 'created' or 'exists' (when not replacing and the name is taken)."""
        if len(data) > SIMPLE_UPLOAD_LIMIT:
            self._session_upload(path, data, replace=replace)
            return "created"
        url = self._url(path, "/content") + "?@microsoft.graph.conflictBehavior=" + ("replace" if replace else "fail")
        for attempt in (0, 1):
            try:
                self._req("PUT", url, data=data, headers={"Content-Type": "application/octet-stream"},
                          expect=(200, 201))
                return "created"
            except NotFound:
                if attempt:
                    raise
                self.ensure_folder(path.rsplit("/", 1)[0] if "/" in path else "")
            except GraphError as e:
                if e.status == 409 and not replace:
                    return "exists"
                raise
        return "created"

    def _session_upload(self, path: str, data: bytes, replace: bool, if_match: Optional[str] = None) -> dict:
        if "/" in path:
            self.ensure_folder(path.rsplit("/", 1)[0])
        headers = {"If-Match": if_match} if if_match else None
        body = {"item": {"@microsoft.graph.conflictBehavior": "replace" if replace else "fail"}}
        sess = self._req("POST", self._url(path, "/createUploadSession"), json=body, headers=headers,
                         expect=(200,)).json()
        upload_url, total, pos, last = sess["uploadUrl"], len(data), 0, None
        while pos < total:
            part = data[pos:pos + CHUNK]
            r = self._req("PUT", upload_url, auth=False, data=part, expect=(200, 201, 202),
                          headers={"Content-Range": f"bytes {pos}-{pos + len(part) - 1}/{total}"})
            pos += len(part)
            if r.status_code in (200, 201):
                last = r.json()
        if last is None:
            raise GraphError(0, "upload finished without a final response")
        return last

    def replace_file(self, path: str, local: Path, if_match: Optional[str] = None) -> dict:
        """Replace/create a file with an upload session; with if_match a concurrent edit raises PreconditionFailed."""
        return self._session_upload(path, Path(local).read_bytes(), replace=True, if_match=if_match)


# ---------------------------------------------------------------------------------------------- config
def auth_from_env(env=os.environ) -> _Auth:
    tenant = env.get("MS_TENANT_ID", "").strip()
    client = env.get("MS_CLIENT_ID", "").strip()
    if not client:
        raise AuthError("MS_CLIENT_ID is not set")
    secret = env.get("MS_CLIENT_SECRET", "").strip()
    rt = env.get("MS_REFRESH_TOKEN", "").strip()
    token_file = env.get("MS_TOKEN_FILE", "").strip() or None
    if rt or (token_file and Path(token_file).exists()):
        return RefreshTokenAuth(tenant or "common", client, rt, token_file, secret)
    if secret:
        if not tenant:
            raise AuthError("MS_TENANT_ID is required for app-only (client secret) access")
        return ClientCredentialsAuth(tenant, client, secret)
    raise AuthError("set either MS_CLIENT_SECRET (app-only) or MS_REFRESH_TOKEN (signed-in user)")


def drive_spec_from_env(env=os.environ) -> str:
    if env.get("ONEDRIVE_DRIVE_ID", "").strip():
        return "id:" + env["ONEDRIVE_DRIVE_ID"].strip()
    if env.get("ONEDRIVE_USER", "").strip():
        return "user:" + env["ONEDRIVE_USER"].strip()
    return "me"
