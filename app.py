import hmac
import json

import requests
import streamlit as st
import streamlit.components.v1 as components


POSTCODE_API = "https://api.postcodes.io/postcodes"
DEFAULT_ZOHO_ACCOUNTS_URL = "https://accounts.zoho.eu"
MODULES = ("Accounts", "Leads", "Deals")
SCOPES = ",".join(
    f"ZohoCRM.modules.{module.lower()}.READ" for module in MODULES
)

ZOHO_ERRORS = {
    "invalid_client": "Check the Client ID and Zoho data centre.",
    "invalid_client_secret": "Check the Client Secret for this Self Client.",
    "access_denied": "Check that your Zoho user has authorised this client.",
}


def normalise_postcode(value):
    return "".join(str(value or "").upper().split())


def field_label(value, fallback):
    return str(value or "").strip() or fallback


def script_json(value):
    return (
        json.dumps(value)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def request_zoho_token(config, grant_type, credential):
    accounts_url = config.get("accounts_url", DEFAULT_ZOHO_ACCOUNTS_URL)
    credential_key = "code" if grant_type == "authorization_code" else "refresh_token"

    response = requests.post(
        f"{accounts_url.strip().rstrip('/')}/oauth/v2/token",
        data={
            "grant_type": grant_type,
            "client_id": config["client_id"].strip(),
            "client_secret": config["client_secret"].strip(),
            credential_key: credential.strip(),
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
    if error == "invalid_code":
        if grant_type == "authorization_code":
            raise RuntimeError(
                "Zoho token exchange failed: invalid_code. Generate a fresh "
                "authorisation code in this Self Client and submit it before it expires."
            )
        raise RuntimeError(
            "Zoho token refresh failed: invalid_code. Generate a replacement "
            "refresh token using Zoho token setup."
        )

    if isinstance(error, str) and error in ZOHO_ERRORS:
        raise RuntimeError(f"Zoho token request failed: {error}. {ZOHO_ERRORS[error]}")

    raise RuntimeError(f"Zoho token request failed (HTTP {response.status_code}).")


def show_zoho_token_setup(config):
    setup_password = config.get("setup_password", "")
    if not isinstance(setup_password, str) or not setup_password:
        return

    with st.expander("Zoho token setup"):
        st.caption("Generate a fresh authorisation code in the same Zoho Self Client with these scopes.")
        st.code(SCOPES, language="text")

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
            token = request_zoho_token(config, "authorization_code", grant_code)
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

        st.success("Replace the refresh_token line under [zoho] in Streamlit Secrets.")
        st.code(f"refresh_token = {json.dumps(refresh_token)}", language="toml")


def read_module(session, api_domain, module, fields):
    url = f"{api_domain.rstrip('/')}/crm/v8/{module}"
    params = {"fields": ",".join(dict.fromkeys(fields)), "per_page": 200}
    records = {}
    seen_tokens = set()
    page = 1

    while True:
        response = session.get(url, params=params, timeout=30, allow_redirects=False)

        if response.status_code == 204:
            break

        if response.status_code in (401, 403):
            raise RuntimeError(
                f"Zoho denied access to {module}. Check the module's read scope "
                "and the connected user's Zoho permissions."
            )

        if response.status_code != 200:
            raise RuntimeError(
                f"Zoho could not load {module} (HTTP {response.status_code}). "
                "Check the configured field API names and try again."
            )

        payload = response.json()
        for record in payload["data"]:
            records[record["id"]] = record

        info = payload["info"]
        if not info["more_records"]:
            break

        next_token = info.get("next_page_token")
        if next_token:
            if next_token in seen_tokens:
                raise RuntimeError(f"Zoho repeated a page token for {module}.")
            seen_tokens.add(next_token)
            params.pop("page", None)
            params["page_token"] = next_token
        elif "page_token" not in params and page < 10:
            page += 1
            params["page"] = page
        else:
            raise RuntimeError(f"Zoho did not return a usable next page for {module}.")

    return list(records.values())


@st.cache_data(ttl=600, show_spinner=False)
def load_map_records(modules):
    config = st.secrets["zoho"]
    refresh_token = config.get("refresh_token", "").strip()
    if not refresh_token:
        raise RuntimeError("Save a refresh_token under [zoho] in Streamlit Secrets.")

    token = request_zoho_token(config, "refresh_token", refresh_token)
    api_domain = token.get("api_domain")
    if not api_domain:
        raise RuntimeError("Zoho did not return the CRM API domain.")

    account_postcode_field = config.get("account_postcode_field", "Post_Code")
    account_type_field = config.get("account_type_field", "Account_Type")
    lead_postcode_field = config.get("lead_postcode_field", "Zip_Code")
    deal_type_field = config.get("deal_type_field", "Type")

    fields_by_module = {
        "Accounts": ("Account_Name", account_postcode_field, account_type_field),
        "Leads": ("Full_Name", "Company", lead_postcode_field),
        "Deals": ("Deal_Name", "Stage", "Account_Name", deal_type_field),
    }

    requested = set(modules)
    if "Deals" in requested:
        requested.add("Accounts")

    raw = {}
    errors = []

    with requests.Session() as session:
        session.headers["Authorization"] = f"Zoho-oauthtoken {token['access_token']}"

        for module in MODULES:
            if module not in requested:
                continue

            try:
                raw[module] = read_module(
                    session, api_domain, module, fields_by_module[module]
                )
            except RuntimeError as error:
                errors.append(str(error))
            except (requests.RequestException, ValueError, KeyError, TypeError):
                errors.append(f"Could not load {module}. Try Refresh Zoho records.")

    records = []
    account_postcodes = {}

    for account in raw.get("Accounts", []):
        postcode = normalise_postcode(account.get(account_postcode_field))
        account_postcodes[account["id"]] = postcode
        if "Accounts" in modules:
            account_type = field_label(account.get(account_type_field), "Not set")
            records.append({
                "id": f"Accounts:{account['id']}",
                "module": "Accounts",
                "name": field_label(account.get("Account_Name"), "Unnamed account"),
                "postcode": postcode,
                "account_type": account_type,
                "detail": f"Account type: {account_type}",
            })

    for lead in raw.get("Leads", []):
        company = field_label(lead.get("Company"), "")
        name = field_label(lead.get("Full_Name"), company or "Unnamed lead")
        records.append({
            "id": f"Leads:{lead['id']}",
            "module": "Leads",
            "name": name,
            "postcode": normalise_postcode(lead.get(lead_postcode_field)),
            "detail": company if company != name else "",
        })

    for deal in raw.get("Deals", []):
        linked_account = deal.get("Account_Name")
        account = linked_account if isinstance(linked_account, dict) else {}
        stage = field_label(deal.get("Stage"), "Not set")
        deal_type = field_label(deal.get(deal_type_field), "Not set")
        details = [f"Stage: {stage}", f"Deal type: {deal_type}"]
        if account.get("name"):
            details.insert(0, account["name"])

        records.append({
            "id": f"Deals:{deal['id']}",
            "module": "Deals",
            "name": field_label(deal.get("Deal_Name"), "Unnamed deal"),
            "postcode": account_postcodes.get(account.get("id"), ""),
            "stage": stage,
            "deal_type": deal_type,
            "detail": " · ".join(details),
        })

    records.sort(key=lambda record: record["name"].casefold())
    return records, errors


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


def choose_filter(records, module, field, label, placeholder):
    options = sorted(
        {record[field] for record in records if record["module"] == module},
        key=str.casefold,
    )
    return st.multiselect(label, options, placeholder=placeholder)


MAP_HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SY Comms CRM Map</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>
    body { margin: 0; font: 16px Arial, sans-serif; }
    form { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
    input, button { font: inherit; padding: 8px 12px; }
    input { width: 10rem; }
    #status { min-height: 1.25em; margin: 8px 0; }
    #map { height: 520px; border-radius: 12px; }
    .layer-dot {
      display: inline-block;
      width: 0.65em;
      height: 0.65em;
      margin-right: 0.35em;
      border-radius: 50%;
    }
  </style>
</head>
<body>
  <form id="postcode-form">
    <label for="postcode">UK postcode</label>
    <input id="postcode" placeholder="SW1A 1AA" maxlength="12" required>
    <button type="submit">Find postcode</button>
  </form>
  <p id="status" role="status"></p>
  <div id="map" aria-label="Map of Zoho CRM records"></div>

  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const pins = __CRM_PINS__;
    const activeModules = __CRM_MODULES__;
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

      const styles = {
        Accounts: { colour: '#2563eb', radius: 7 },
        Leads: { colour: '#15803d', radius: 12 },
        Deals: { colour: '#c2410c', radius: 17 }
      };

      const layers = {};
      const overlays = {};
      for (const module of activeModules) {
        layers[module] = L.layerGroup().addTo(map);
        const colour = styles[module].colour;
        const label = `<span class="layer-dot" style="background:${colour}"></span>${module}`;
        overlays[label] = layers[module];
      }

      // Group records by coordinates and module so shared locations stay clickable.
      const locations = new Map();
      for (const pin of pins) {
        const key = JSON.stringify(pin.location);
        if (!locations.has(key)) locations.set(key, new Map());
        const groups = locations.get(key);
        if (!groups.has(pin.module)) groups.set(pin.module, []);
        groups.get(pin.module).push(pin);
      }

      const bounds = L.latLngBounds([]);
      for (const groups of locations.values()) {
        const shared = groups.size > 1;

        for (const [module, records] of groups) {
          const location = records[0].location;
          const style = styles[module];

          const popup = document.createElement('div');
          const heading = document.createElement('strong');
          heading.textContent = module;
          popup.append(heading);

          const list = document.createElement('ul');
          for (const record of records) {
            const item = document.createElement('li');
            item.textContent = [record.name, record.postcode, record.detail]
              .filter(Boolean).join(' · ');
            list.append(item);
          }
          popup.append(list);

          L.circleMarker(location, {
            radius: shared ? style.radius : 8,
            color: style.colour,
            fillColor: style.colour,
            fill: !shared,
            fillOpacity: 0.65,
            weight: 3
          }).bindPopup(popup, { maxHeight: 240 }).addTo(layers[module]);

          bounds.extend(location);
        }
      }

      if (activeModules.length) {
        L.control.layers({}, overlays, { collapsed: false }).addTo(map);
      }
      if (bounds.isValid()) {
        map.fitBounds(bounds, { padding: [35, 35], maxZoom: 14 });
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
          postcodePin = L.circleMarker(location, {
            radius: 10,
            color: '#7c3aed'
          }).addTo(map).bindPopup(popup).openPopup();
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

modules = []
pins = []

try:
    config = st.secrets["zoho"]
except (FileNotFoundError, KeyError):
    st.error("Add the [zoho] section in Streamlit → App settings → Secrets.")
else:
    show_zoho_token_setup(config)

    if st.button("Refresh Zoho records"):
        load_map_records.clear()

    modules = st.multiselect("Map layers", MODULES, default=["Accounts"])

    records = []
    if modules:
        try:
            with st.spinner("Reading Zoho records…"):
                records, errors = load_map_records(tuple(sorted(modules)))
            for error in errors:
                st.error(error)
        except RuntimeError as error:
            st.error(str(error))
        except KeyError:
            st.error("Check the required [zoho] settings in Streamlit Secrets.")
        except (requests.RequestException, ValueError, TypeError):
            st.error("The Zoho request failed. Click Refresh Zoho records to retry.")

    account_types = []
    deal_types = []
    deal_stages = []

    if "Accounts" in modules:
        account_types = choose_filter(
            records, "Accounts", "account_type", "Account types", "All account types"
        )

    if "Deals" in modules:
        deal_types = choose_filter(
            records, "Deals", "deal_type", "Deal types", "All deal types"
        )
        deal_stages = choose_filter(
            records, "Deals", "stage", "Deal stages", "All deal stages"
        )

    selected = []
    for record in records:
        module = record["module"]
        if module == "Accounts" and account_types:
            if record["account_type"] not in account_types:
                continue
        if module == "Deals":
            if deal_types and record["deal_type"] not in deal_types:
                continue
            if deal_stages and record["stage"] not in deal_stages:
                continue
        selected.append(record)

    if modules:
        mode = st.radio(
            "Show on map",
            ["All matching records", "Select individual records"],
            horizontal=True,
        )

        if mode == "Select individual records":
            by_id = {record["id"]: record for record in selected}
            labels = {
                record_id: f"{record['name']} ({record['module']})"
                for record_id, record in by_id.items()
            }
            chosen = st.multiselect(
                "Search and select records",
                list(by_id),
                format_func=labels.get,
            )
            selected = [by_id[record_id] for record_id in chosen if record_id in by_id]

        postcodes = sorted({
            record["postcode"] for record in selected if record["postcode"]
        })

        try:
            with st.spinner("Looking up postcodes…"):
                locations = lookup_postcodes(postcodes)

            pins = [
                {
                    "name": record["name"],
                    "module": record["module"],
                    "detail": record["detail"],
                    **locations[record["postcode"]],
                }
                for record in selected
                if record["postcode"] in locations
            ]

            for module in modules:
                layer_records = [
                    record for record in selected if record["module"] == module
                ]
                mapped = sum(pin["module"] == module for pin in pins)
                missing = sum(not record["postcode"] for record in layer_records)
                unmatched = len(layer_records) - mapped - missing
                missing_label = (
                    "no linked Account postcode" if module == "Deals" else "no postcode"
                )
                st.caption(
                    f"{module} · {mapped:,} mapped · {missing:,} {missing_label} · "
                    f"{unmatched:,} postcode not found"
                )
        except (requests.RequestException, ValueError, KeyError, TypeError):
            st.error("The postcode lookup failed. Refresh the page to retry.")

html = MAP_HTML.replace("__CRM_MODULES__", script_json(modules), 1)
html = html.replace("__CRM_PINS__", script_json(pins), 1)
components.html(html, height=650)
