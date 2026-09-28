from hdbcli import dbapi


class HanaConnection:

    def __init__(self, hana_config: dict):
        self.hana = hana_config
        self.schema = hana_config['schema']

    # Fail fast instead of hanging a request worker indefinitely when SAP is
    # unreachable or a round-trip stalls. Both values are in milliseconds.
    CONNECT_TIMEOUT_MS = 15000
    COMMUNICATION_TIMEOUT_MS = 60000

    def connect(self):
        from sap_client import health

        # Known down and probed moments ago: fail now instead of after the
        # connect timeout. Raised as the error a refused connection gives, so
        # every caller's `except dbapi.Error` treats it exactly as it would the
        # real thing -- a fail-soft page stays fail-soft, only faster.
        state = health.state_for_call(health.HANA)
        if health.failing_fast(state):
            raise dbapi.OperationalError(-10709, health.refusal(health.HANA, state))

        conn = dbapi.connect(
            address=self.hana['host'],
            port=self.hana['port'],
            user=self.hana['user'],
            password=self.hana['password'],
            connectTimeout=self.CONNECT_TIMEOUT_MS,
            communicationTimeout=self.COMMUNICATION_TIMEOUT_MS,
        )
        health.record_success(health.HANA, state)
        return conn


# The same limits, for the call sites that open their own connection with
# ``dbapi.connect`` rather than through a HanaConnection. Without them a HANA
# that accepts and stalls holds a gunicorn worker until the OS gives up.
HANA_TIMEOUTS = {
    "connectTimeout": HanaConnection.CONNECT_TIMEOUT_MS,
    "communicationTimeout": HanaConnection.COMMUNICATION_TIMEOUT_MS,
}
