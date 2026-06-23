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
        return dbapi.connect(
            address=self.hana['host'],
            port=self.hana['port'],
            user=self.hana['user'],
            password=self.hana['password'],
            connectTimeout=self.CONNECT_TIMEOUT_MS,
            communicationTimeout=self.COMMUNICATION_TIMEOUT_MS,
        )
