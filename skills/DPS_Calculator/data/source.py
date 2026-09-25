"""Which data source the DPS Calculator reads, and the erkul network gate.

The calculator was built on server.erkul.games. That host no longer resolves,
and erkul's API terms say third-party automated access is not authorized, so
the default source is StarCitizenWiki/scunpacked-data (data/scunpacked_provider.py).

The erkul code path is kept (repository.py, api_client.py, erkul_item_resolver.py)
but is unreachable: every erkul network call asks ``erkul_network_allowed()``,
which is False unless BOTH constants below are changed in code. There is no
setting, flag or environment variable that turns it back on.
"""

SOURCE = "scunpacked"          # "scunpacked" (default) | "erkul" (legacy, offline only)
ERKUL_NETWORK = False          # never send requests to erkul.games

ATTRIBUTION = ("Ship and weapon data: StarCitizenWiki/scunpacked-data. "
               "Calculator lineage: erkul.games. Star Citizen content (c) "
               "Cloud Imperium Games; not affiliated.")


def use_scunpacked() -> bool:
    return SOURCE != "erkul"


def erkul_network_allowed() -> bool:
    return SOURCE == "erkul" and ERKUL_NETWORK is True


class ErkulNetworkDisabled(RuntimeError):
    """Raised instead of making a request to erkul.games."""

    def __init__(self, what: str = "") -> None:
        super().__init__("erkul.games requests are disabled (third-party automated "
                         "access is not authorized); data comes from scunpacked-data"
                         + (f" [{what}]" if what else ""))
