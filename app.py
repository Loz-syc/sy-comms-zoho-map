import json

import requests
import streamlit as st
import streamlit.components.v1 as components


POSTCODE_API = "https://api.postcodes.io/postcodes"


def normalise_postcode(value):
    return "".join(str(value or "").upper().split())


@st.cache_data(ttl=600, show_spinner=False)
def load_accounts():
    config = st.secrets["zoho"]
    accounts_url = config.get("accounts_url", "https://accounts.zoho.eu")

    with requests.Session() as session:
        response = session.post(
            f"{accounts_url.rstrip('/')}/oauth/v2/token",
            data={
                "grant_type": "refresh_token",
                "client_id": config["client_id"],
                "client_secret": config["client_secret"],
                "refresh_token": config["refresh_token"],
            },
            timeout=20,
        )
        token = response.json()
        if not response.ok or not token.get("access_token"):
            raise RuntimeError(
                "Zoho rejected the credentials. Check that the client ID, "
                "client secret and refresh token belong to the same Zoho client "
                "and that the Zoho data centre is correct."
            )

        session.headers["Authorization"] = (
            f"Zoho-oauthtoken {token['access_token']}"
        )
        api_url = f"{token['api_domain'].rstrip('/')}/crm/v8/Accounts"
        params = {"fields": "Account_Name,Post_Code", "per_page": 200}
        records = {}
        page = 1

        while True:
            response = session.get(api_url, params=params, timeout=30)
            if response.status_code == 204:
                break
            if response.status_code in (401, 403):
                raise RuntimeError(
                    "Zoho denied access to Accounts. Check the token's Accounts "
                    "read scope and the Zoho user's permissions."
                )
            response.raise_for_status()
            payload = response.json()

            for record in payload["data"]:
                records[record["id"]] = {
                    "id": record["id"],
                    "name": record.get("Account_Name") or "Unnamed account",
                    "postcode": normalise_postcode(record.get("Post_Code")),
                }

            info = payload["info"]
            if not info["more_records"]:
                break

            if info.get("next_page_token"):
                params.pop("page", None)
                params["page_token"] = info["next_page_token"]
            elif "page_token" not in params and page < 10:
                page += 1
                params["page"] = page
            else:
                raise RuntimeError("Zoho did not return the next page of Accounts.")

    return sorted(records.values(), key=lambda item: item["name"].casefold())


@st.cache_data(ttl=86400, show_spinner=False)
def lookup_postcodes(postcodes):
    locations = {}
    for start in range(0, len(postcodes), 100):
        response = requests.post(
            POSTCODE_API,
            json={"postcodes": postcodes[start:start + 100]},
            timeout=20,
        )
        response.raise_for_status()
        for match in response.json()["result"]:
            result = match.get("result")
            if result and result.get("latitude") is not None and result.get("longitude") is not None:
                locations[normalise_postcode(match["query"])] = {
                    "postcode": result["postcode"],
                    "location": [result["latitude"], result["longitude"]],
                }
    return locations


st.set_page_config(page_title="SY Comms CRM Map", layout="wide")
st.title("SY Comms CRM Map")

if st.button("Refresh Zoho accounts"):
    load_accounts.clear()

pins = []
try:
    with st.spinner("Reading Zoho Accounts…"):
        accounts = load_accounts()
except RuntimeError as error:
    st.error(str(error))
except (FileNotFoundError, KeyError):
    st.error("Check that the [zoho] section and all three credentials are saved in Streamlit Secrets.")
except (requests.RequestException, ValueError):
    st.error("The Zoho request failed. Try Refresh Zoho accounts.")

if "accounts" in locals():
    mode = st.radio(
        "Show on map",
        ["Select accounts", "All accounts"],
        horizontal=True,
    )
    if mode == "Select accounts":
        by_id = {account["id"]: account for account in accounts}
        labels = {
            account["id"]: f"{account['name']} ({account['postcode'] or 'no postcode'})"
            for account in accounts
        }
        chosen = st.multiselect("Choose accounts", list(by_id), format_func=labels.get)
        selected = [by_id[account_id] for account_id in chosen]
    else:
        selected = accounts

    postcodes = sorted({account["postcode"] for account in selected if account["postcode"]})
    missing = sum(not account["postcode"] for account in selected)

    try:
        with st.spinner("Looking up account postcodes…"):
            locations = lookup_postcodes(postcodes)
        pins = [
            {"name": account["name"], **locations[account["postcode"]]}
            for account in selected
            if account["postcode"] in locations
        ]
        st.caption(
            f"{len(accounts):,} read from Zoho · {len(selected):,} selected · "
            f"{len(pins):,} pinned · {missing:,} missing postcode · "
            f"{len(selected) - missing - len(pins):,} postcode not found"
        )
    except (requests.RequestException, ValueError, KeyError, TypeError):
        st.error("The account postcode lookup failed. Reload the page to retry.")

# Escape HTML-sensitive characters before inserting JSON into the script.
pins_json = json.dumps(pins).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")

MAP_HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>
    body { margin: 0; font: 16px Arial, sans-serif; }
    form { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
    input, button { font: inherit; padding: 8px 12px; }
    input { width: 10rem; }
    #status { min-height: 1.25em; margin: 8px 0; }
    #map { height: 520px; border-radius: 12px; }
  </style>
</head>
<body>
  <form id="postcode-form">
    <label for="postcode">UK postcode</label>
    <input id="postcode" placeholder="SW1A 1AA" maxlength="12" required>
    <button type="submit">Find postcode</button>
  </form>
  <p id="status" role="status"></p>
  <div id="map" aria-label="Map of Zoho accounts"></div>

  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const pins = __ACCOUNT_PINS__;
    const form = document.querySelector('#postcode-form');
    const input = document.querySelector('#postcode');
    const button = form.querySelector('button');
    const statusMessage = document.querySelector('#status');

    if (typeof L === 'undefined') {
      statusMessage.textContent = 'The map could not load. Refresh the page to retry.';
      button.disabled = true;
    } else {
      initialiseMap();
    }

    function initialiseMap() {
      const map = L.map('map').setView([54.2, -2.9], 6);
      L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
        maxZoom: 19,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
      }).addTo(map);

      // Put accounts sharing one postcode in the same popup.
      const groups = new Map();
      for (const pin of pins) {
        if (!groups.has(pin.postcode)) {
          groups.set(pin.postcode, { location: pin.location, names: [] });
        }
        groups.get(pin.postcode).names.push(pin.name);
      }

      const accountLayer = L.featureGroup().addTo(map);
      for (const [postcode, group] of groups) {
        const popup = document.createElement('div');
        const heading = document.createElement('strong');
        heading.textContent = postcode;
        popup.append(heading);

        const list = document.createElement('ul');
        for (const name of group.names) {
          const item = document.createElement('li');
          item.textContent = name;
          list.append(item);
        }
        popup.append(list);
        L.marker(group.location).bindPopup(popup, { maxHeight: 240 }).addTo(accountLayer);
      }

      if (groups.size) {
        map.fitBounds(accountLayer.getBounds(), { padding: [35, 35], maxZoom: 14 });
      }

      let postcodePin;
      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        const postcode = input.value.toUpperCase().replace(/\s+/g, '');
        if (!postcode || button.disabled) return;

        button.disabled = true;
        statusMessage.textContent = 'Looking up postcode…';
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 10000);

        try {
          const response = await fetch(
            'https://api.postcodes.io/postcodes/' + encodeURIComponent(postcode),
            { signal: controller.signal }
          );
          if (response.status === 400 || response.status === 404) {
            statusMessage.textContent = 'Postcode not found. Check it and try again.';
            return;
          }
          if (!response.ok) throw new Error('Lookup failed');

          const { result } = await response.json();
          const location = [result?.latitude, result?.longitude];
          if (!location.every(Number.isFinite)) throw new Error('Missing coordinates');

          const popup = document.createElement('span');
          popup.textContent = result.postcode;
          if (postcodePin) postcodePin.remove();
          map.setView(location, 14);
          postcodePin = L.circleMarker(location, { radius: 10, color: '#7c3aed' })
            .addTo(map).bindPopup(popup).openPopup();
          statusMessage.textContent = 'Showing ' + result.postcode;
        } catch {
          statusMessage.textContent = 'Postcode lookup is unavailable. Try again shortly.';
        } finally {
          clearTimeout(timer);
          button.disabled = false;
        }
      });
    }
  </script>
</body>
</html>
"""

components.html(MAP_HTML.replace("__ACCOUNT_PINS__", pins_json), height=650)
