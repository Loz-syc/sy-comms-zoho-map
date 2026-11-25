import hmac
import json

import requests
import streamlit as st
import streamlit.components.v1 as components


POSTCODE_API = "https://api.postcodes.io/postcodes"
DEFAULT_ZOHO_ACCOUNTS_URL = "https://accounts.zoho.eu"

ZOHO_ERRORS = {
    "invalid_client": "Check the Client ID and Zoho data centre.",
    "invalid_client_secret": "Check the Client Secret for this Self Client.",
    "invalid_code": "Zoho rejected the code or refresh token.",
    "access_denied": "Check that the Zoho user has authorised this client.",
}


def normalise_postcode(value):
    return "".join(str(value or "").upper().split())


def request_zoho_token(config, grant_type, credential_name, credential):
    accounts_url = config.get("accounts_url", DEFAULT_ZOHO_ACCOUNTS_URL)
    response = requests.post(
        f"{accounts_url.strip().rstrip('/')}/oauth/v2/token",
        data={
            "grant_type": grant_type,
            "client_id": config["client_id"].strip(),
            "client_secret": config["client_secret"].strip(),
            credential_name: credential.strip(),
        },
        timeout=20,
        allow_redirects=False,
    )

    try:
        result = response.json()
    except ValueError:
        result = {}

    if isinstance(result, dict) and response.ok and result.get("access_token"):
        return result

    error = result.get("error") if isinstance(result, dict) else None
    if isinstance(error, str) and error in ZOHO_ERRORS:
        if error == "invalid_code":
            if grant_type == "authorization_code":
                raise RuntimeError(
                    "Zoho token exchange failed: invalid_code. Generate a fresh "
                    "authorisation code in the same Self Client and submit it "
                    "before it expires."
                )
            raise RuntimeError(
                "Zoho token refresh failed: invalid_code. Use Zoho token setup "
                "to generate a replacement refresh token."
            )
        raise RuntimeError(f"Zoho token request failed: {error}. {ZOHO_ERRORS[error]}")

    raise RuntimeError(
        f"Zoho token request failed (HTTP {response.status_code}). "
        "Check the Zoho Accounts URL and try again."
    )


def show_zoho_token_setup(config):
    setup_password = config.get("setup_password", "")
    if not isinstance(setup_password, str) or not setup_password:
        return

    with st.expander("Zoho token setup"):
        st.caption(
            "In the same Zoho Self Client, generate a fresh authorisation code "
            "with the scope ZohoCRM.modules.accounts.READ."
        )

        with st.form("zoho_token_setup", clear_on_submit=True):
            password = st.text_input("Setup password", type="password")
            grant_code = st.text_input("Zoho authorisation code", type="password")
            submitted = st.form_submit_button("Generate refresh token")

        if not submitted:
            return

        if not hmac.compare_digest(password.encode(), setup_password.encode()):
            st.error("The setup password is incorrect.")
            return

        if not grant_code.strip():
            st.error("Enter a fresh Zoho authorisation code.")
            return

        try:
            token = request_zoho_token(
                config, "authorization_code", "code", grant_code
            )
        except KeyError:
            st.error("Add client_id and client_secret under [zoho] in Streamlit Secrets.")
            return
        except RuntimeError as error:
            st.error(str(error))
            return
        except requests.RequestException:
            st.error("Could not reach Zoho. Check the Accounts URL and try a fresh code.")
            return

        refresh_token = token.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            st.error("Zoho accepted the code but did not return a refresh token.")
            return

        st.success("Copy this line into [zoho] in Streamlit Secrets, replacing the old line.")
        st.code(f"refresh_token = {json.dumps(refresh_token)}", language="toml")


@st.cache_data(ttl=600, show_spinner=False)
def load_accounts():
    config = st.secrets["zoho"]
    refresh_token = config.get("refresh_token", "").strip()
    if not refresh_token:
        raise RuntimeError("Save a refresh_token under [zoho] in Streamlit Secrets.")

    token = request_zoho_token(
        config, "refresh_token", "refresh_token", refresh_token
    )
    api_domain = token.get("api_domain")
    if not api_domain:
        raise RuntimeError("Zoho did not return the CRM API domain.")

    api_url = f"{api_domain.rstrip('/')}/crm/v8/Accounts"
    params = {"fields": "Account_Name,Post_Code", "per_page": 200}
    records = {}
    page = 1
    seen_page_tokens = set()

    with requests.Session() as session:
        session.headers["Authorization"] = f"Zoho-oauthtoken {token['access_token']}"

        while True:
            response = session.get(api_url, params=params, timeout=30)
            if response.status_code == 204:
                break
            if response.status_code in (401, 403):
                raise RuntimeError(
                    "Zoho issued an access token but denied access to Accounts. "
                    "Check the Accounts read scope and the Zoho user's permissions."
                )
            response.raise_for_status()
            payload = response.json()

            for record in payload["data"]:
                record_id = record["id"]
                records[record_id] = {
                    "id": record_id,
                    "name": record.get("Account_Name") or "Unnamed account",
                    "postcode": normalise_postcode(record.get("Post_Code")),
                }

            info = payload["info"]
            if not info["more_records"]:
                break

            next_token = info.get("next_page_token")
            if next_token and next_token not in seen_page_tokens:
                seen_page_tokens.add(next_token)
                params.pop("page", None)
                params["page_token"] = next_token
            elif not next_token and "page_token" not in params and page < 10:
                page += 1
                params["page"] = page
            else:
                raise RuntimeError("Zoho did not return a usable next page of Accounts.")

    return sorted(records.values(), key=lambda account: account["name"].casefold())


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
            if not result:
                continue

            latitude = result.get("latitude")
            longitude = result.get("longitude")
            if latitude is not None and longitude is not None:
                locations[normalise_postcode(match["query"])] = {
                    "postcode": result["postcode"],
                    "location": [latitude, longitude],
                }

    return locations


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

        L.marker(group.location)
          .bindPopup(popup, { maxHeight: 240 })
          .addTo(accountLayer);
      }

      if (groups.size) {
        map.fitBounds(accountLayer.getBounds(), { padding: [35, 35], maxZoom: 14 });
      }

      let postcodePin;

      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        if (button.disabled) return;

        const postcode = input.value.toUpperCase().replace(/\s+/g, '');
        if (!postcode) {
          statusMessage.textContent = 'Enter a UK postcode.';
          return;
        }

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
            .addTo(map)
            .bindPopup(popup)
            .openPopup();
          statusMessage.textContent = 'Showing ' + result.postcode;
        } catch {
          statusMessage.textContent = 'The postcode lookup is unavailable. Try again shortly.';
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


st.set_page_config(page_title="SY Comms CRM Map", layout="wide")
st.title("SY Comms CRM Map")

try:
    zoho_config = st.secrets["zoho"]
except (FileNotFoundError, KeyError):
    zoho_config = None
    st.error("Add the [zoho] section in Streamlit App settings → Secrets.")

accounts = None
pins = []

if zoho_config is not None:
    show_zoho_token_setup(zoho_config)

    if st.button("Refresh Zoho accounts"):
        load_accounts.clear()

    try:
        with st.spinner("Reading Zoho Accounts…"):
            accounts = load_accounts()
    except RuntimeError as error:
        st.error(str(error))
    except KeyError:
        st.error("Check the [zoho] settings. A required setting or Zoho response field is missing.")
    except (requests.RequestException, ValueError):
        st.error("The Zoho request failed. Click Refresh Zoho accounts to try again.")

if accounts is not None:
    mode = st.radio("Show on map", ["Select accounts", "All accounts"], horizontal=True)

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

    postcodes = sorted({
        account["postcode"] for account in selected if account["postcode"]
    })
    missing = sum(not account["postcode"] for account in selected)

    try:
        with st.spinner("Looking up account postcodes…"):
            locations = lookup_postcodes(postcodes)

        pins = [
            {"name": account["name"], **locations[account["postcode"]]}
            for account in selected
            if account["postcode"] in locations
        ]
        not_found = len(selected) - missing - len(pins)
        st.caption(
            f"{len(accounts):,} read from Zoho · {len(selected):,} selected · "
            f"{len(pins):,} accounts mapped · {missing:,} missing postcode · "
            f"{not_found:,} postcode not found"
        )
    except (requests.RequestException, ValueError, KeyError, TypeError):
        st.error("The account postcode lookup failed. Reload the page to try again.")

pins_json = (
    json.dumps(pins)
    .replace("<", "\\u003c")
    .replace(">", "\\u003e")
    .replace("&", "\\u0026")
)
components.html(MAP_HTML.replace("__ACCOUNT_PINS__", pins_json), height=650)
