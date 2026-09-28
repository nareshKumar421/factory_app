import requests

from ..exceptions import SAPUnavailable


class ServiceLayerSession:

    def __init__(self, sl_config: dict):
        self.sl = sl_config

    def login(self):
        from .. import health

        # Known down and probed moments ago: say so now, rather than hold the
        # operator for the login timeout to find out what the probe knew.
        state = health.state_for_call(health.SERVICE_LAYER)
        if health.failing_fast(state):
            raise SAPUnavailable(health.refusal(health.SERVICE_LAYER, state))

        response = requests.post(
            f"{self.sl['base_url']}/b1s/v2/Login",
            json={
                "CompanyDB": self.sl["company_db"],
                "UserName": self.sl["username"],
                "Password": self.sl["password"],
            },
            timeout=10,
            verify=False
        )
        response.raise_for_status()
        health.record_success(health.SERVICE_LAYER, state)
        return response.cookies
