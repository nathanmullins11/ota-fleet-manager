"""Client for the visual demo helper running next to the control plane (demo/visual/helper)."""
import json
import urllib.error
import urllib.request


class HelperError(Exception):
    pass


class Helper:
    def __init__(self, url: str, password: str):
        self.url = url.rstrip("/")
        self.password = password

    def _call(self, method: str, path: str, body=None, timeout: float = 8):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.url}{path}", data=data, method=method,
                                     headers={"X-Demo-Password": self.password,
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            try:
                detail = json.load(exc).get("detail", exc.reason)
            except ValueError:
                detail = exc.reason
            if exc.code == 401:
                detail = "the helper rejected the password (VISUAL_DEMO_PASSWORD)"
            raise HelperError(str(detail)) from None
        except (OSError, ValueError) as exc:
            raise HelperError(f"cannot reach the control plane helper at {self.url}: {exc}") from None

    def state(self) -> dict:
        return self._call("GET", "/api/state")

    def start_rollout(self, version: str, devices=None, rollback_to=None) -> dict:
        return self._call("POST", "/api/rollouts", {"version": version, "devices": devices,
                                                   "rollback_to": rollback_to})

    def rollback_latest(self, reason: str) -> dict:
        return self._call("POST", "/api/rollouts/latest/rollback", {"reason": reason})
