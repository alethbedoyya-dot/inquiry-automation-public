"""
Local configuration template.

Copy this file to user_config.py, then fill in private account and deployment
values. user_config.py is ignored by Git and must not be committed.
"""

# Accounts
OMS_USERNAME = ""
OMS_PASSWORD = ""
SPAREPARTS_USERNAME = ""
SPAREPARTS_PASSWORD = ""

# Optional: reuse a browser profile with existing login state.
EDGE_USER_DATA_DIR = ""
SOURCING_KEYWORD = ""

# Private deployment URLs. Public defaults in config.py are placeholders.
OMS_HOME_URL = "https://example-oms.local"
OMS_URL = "https://example-oms.local/path/to/oms/list"
SPAREPARTS_URL = "https://example-spareparts.local/login"
SPAREPARTS_HOST_KEYWORD = "example-spareparts.local"

# Supplier / factory mapping for your deployment.
OMS_SUPPLIER_SHANGHAI = "Supplier A"
OMS_SUPPLIER_ZHONGSHAN = "Supplier B"
OMS_SUPPLIER_EXTEND_SEARCH_KEY = "Supplier"
OMS_SUPPLIER_ID_SONGJIANG = ""
OMS_SUPPLIER_ID_ZHONGSHAN = ""
OMS_PHASE1_SUPPLIER_IDS = [
    OMS_SUPPLIER_ID_SONGJIANG,
    OMS_SUPPLIER_ID_ZHONGSHAN,
]

SONGJIANG_FACTORY_NAME = "Factory A"
ZHONGSHAN_FACTORY_NAME = "Factory B"
