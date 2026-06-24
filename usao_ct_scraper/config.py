BASE_URL = "https://www.justice.gov"
LISTING_URL = f"{BASE_URL}/usao-ct/pr"

OUTPUT_FILE = "press_releases.json"
HEADLESS = True
TIMEOUT = 30000
MAX_PAGES = None  # Set to int to limit pages, None for all
RATE_LIMIT_DELAY = 2.0  # seconds between requests
