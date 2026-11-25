import hmac
import json
import math
from collections import Counter

import requests
import streamlit as st
import streamlit.components.v1 as components


MODULES = ("Accounts", "Leads", "Deals")
SCOPES = ",".join(f"ZohoCRM.modules.{module.lower()}.READ" for module in MODULES)
GB_COUNTRIES = ("England", "Scotland", "Wales")
POSTCODE_API = "https://api.postcodes.io/postcodes"
DEFAULT_ACCOUNTS_URL = "https://accounts.zoho.eu"
TOKEN_ERRORS = {
    "invalid_client": "Check the Client ID and Zoho data centre.",
    "invalid_client_secret": "Check the Client Secret for this Self Client.",
    "access_denied": "Check that your Zoho user authorised this client.",
}


def normalise_postcode(value):
    return "".join(str(value or "").upper().split())


def field_label(value, fallback="Not set"):
    return str(value or "").strip() or fallback


def script_json(value):
    return (
        json.dumps(value, allow_nan=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def request_zoho_token(config, grant_type, credential):
    accounts_url = config.get("accounts_url", DEFAULT_ACCOUNTS_URL).strip().rstrip("/")
    credential_field = (
        "code" if grant_type == "authorization_code" else "refresh_token"
    )
    response = requests.post(
        f"{accounts_url}/oauth/v2/token",
        data={
            "grant_type": grant_type,
            "client_id": config["client_id"].strip(),
            "client_secret": config["client_secret"].strip(),
            credential_field: credential.strip(),
        },
        timeout=20,
        allow_redirects=False,
    )

    try:
        result = response.json()
    except ValueError:
        result = {}

    if not isinstance(result, dict):
        result = {}
    if response.status_code == 200 and result.get("access_token"):
        return result

    error = result.get("error")
    if error == "invalid_code":
        if grant_type == "authorization_code":
            raise RuntimeError(
                "Zoho token exchange failed: invalid_code. Generate a fresh "
                "authorisation code in the same Self Client."
            )
        raise RuntimeError(
            "Zoho token refresh failed: invalid_code. Use Zoho token setup "
            "to generate a replacement refresh token."
        )
    if isinstance(error, str) and error in TOKEN_ERRORS:
        raise RuntimeError(f"Zoho token request failed: {error}. {TOKEN_ERRORS[error]}")
    raise RuntimeError(f"Zoho token request failed (HTTP {response.status_code}).")


def show_token_setup(config):
    setup_password = config.get("setup_password", "")
    if not isinstance(setup_password, str) or not setup_password:
        return

    with st.expander("Zoho token setup"):
        st.caption("Generate a fresh authorisation code in your Zoho Self Client.")
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
            st.error("Zoho did not return a refresh token.")
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
                f"Zoho denied access to {module}. Check the read scope "
                "and the connected user's module permissions."
            )
        if response.status_code != 200:
            raise RuntimeError(
                f"Zoho could not load {module} (HTTP {response.status_code}). "
                "Check the configured field API names."
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
    deal_type_field = config.get("deal_type_field", "Deal_Type")

    fields = {
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
                raw[module] = read_module(session, api_domain, module, fields[module])
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
            account_type = field_label(account.get(account_type_field))
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
        linked = deal.get("Account_Name")
        account = linked if isinstance(linked, dict) else {}
        stage = field_label(deal.get("Stage"))
        deal_type = field_label(deal.get(deal_type_field))
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
    outside_gb = set()

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

            query = normalise_postcode(match["query"])
            if result.get("country") not in GB_COUNTRIES:
                outside_gb.add(query)
                continue

            latitude = result.get("latitude")
            longitude = result.get("longitude")
            if latitude is None or longitude is None:
                continue
            if not math.isfinite(latitude) or not math.isfinite(longitude):
                continue

            locations[query] = {
                "postcode": result["postcode"],
                "location": [latitude, longitude],
            }

    return locations, outside_gb


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
    #postcode, button { font: inherit; padding: 8px 12px; }
    #postcode { width: 10rem; }
    button { cursor: pointer; }
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
    <label for="postcode">Postcode or prefix</label>
    <input id="postcode" placeholder="TF or TF1 1AA" maxlength="12"
           autocomplete="off" spellcheck="false">
    <button type="submit">Search</button>
    <button type="button" id="clear-postcode">Clear</button>
  </form>
  <p id="status" role="status"></p>
  <div id="map" aria-label="Map of Zoho CRM records in Great Britain"></div>

  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const { pins, modules, countries, postcodeApi } = __CRM_DATA__;
    const gbCountries = new Set(countries);
    const form = document.querySelector('#postcode-form');
    const input = document.querySelector('#postcode');
    const searchButton = form.querySelector('button[type="submit"]');
    const clearButton = document.querySelector('#clear-postcode');
    const statusMessage = document.querySelector('#status');

    if (typeof L === 'undefined') {
      statusMessage.textContent = 'The map could not load. Refresh the page to retry.';
      searchButton.disabled = true;
      clearButton.disabled = true;
    } else {
      initialiseMap();
    }

    function normalisePostcode(value) {
      return value.toUpperCase().replace(/\s+/g, '');
    }

    function buildPopup(module, records) {
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
      return popup;
    }

    function initialiseMap() {
      const gbBounds = L.latLngBounds([49.8, -8.8], [61.0, 2.1]);
      const map = L.map('map', {
        maxBounds: gbBounds,
        maxBoundsViscosity: 1,
        minZoom: 4
      }).fitBounds(gbBounds);

      L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
        maxZoom: 19,
        noWrap: true,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
      }).addTo(map);

      const styles = {
        Accounts: { colour: '#2563eb', radius: 7 },
        Leads: { colour: '#15803d', radius: 12 },
        Deals: { colour: '#c2410c', radius: 17 }
      };
      const layers = new Map();
      const overlays = {};

      for (const module of modules) {
        const layer = L.layerGroup().addTo(map);
        layers.set(module, layer);
        const colour = styles[module].colour;
        const label = `<span class="layer-dot" style="background:${colour}"></span>${module}`;
        overlays[label] = layer;
      }

      const locations = new Map();
      for (const pin of pins) {
        const key = JSON.stringify(pin.location);
        if (!locations.has(key)) locations.set(key, new Map());
        const groups = locations.get(key);
        if (!groups.has(pin.module)) groups.set(pin.module, []);
        groups.get(pin.module).push(pin);
      }

      const crmMarkers = [];
      for (const groups of locations.values()) {
        const shared = groups.size > 1;
        for (const [module, records] of groups) {
          const style = styles[module];
          const marker = L.circleMarker(records[0].location, {
            radius: shared ? style.radius : 8,
            color: style.colour,
            fillColor: style.colour,
            fill: !shared,
            fillOpacity: 0.65,
            weight: 3
          }).bindPopup(buildPopup(module, records), { maxHeight: 240 });

          crmMarkers.push({ marker, module, records, layer: layers.get(module) });
        }
      }

      if (modules.length) {
        L.control.layers({}, overlays, { collapsed: false }).addTo(map);
      }

      let activePrefix = '';
      let postcodePin;

      function showCrmMarkers(prefix) {
        const bounds = L.latLngBounds([]);
        let count = 0;

        for (const { marker, module, records, layer } of crmMarkers) {
          const matches = prefix
            ? records.filter(record => normalisePostcode(record.postcode).startsWith(prefix))
            : records;

          if (!matches.length) {
            layer.removeLayer(marker);
            continue;
          }

          marker.setPopupContent(buildPopup(module, matches));
          if (!layer.hasLayer(marker)) layer.addLayer(marker);

          if (map.hasLayer(layer)) {
            bounds.extend(marker.getLatLng());
            count += matches.length;
          }
        }

        return { bounds, count };
      }

      function fitRecords(bounds) {
        map.fitBounds(bounds.isValid() ? bounds : gbBounds, {
          padding: [35, 35], maxZoom: 14
        });
      }

      function describePrefix(count) {
        statusMessage.textContent = count
          ? `${count} visible CRM record${count === 1 ? '' : 's'} with postcodes starting ${activePrefix}.`
          : `No visible CRM records have postcodes starting ${activePrefix}.`;
      }

      function removePostcodePin() {
        if (postcodePin) postcodePin.remove();
        postcodePin = undefined;
      }

      function clearPostcodeSearch() {
        input.value = '';
        activePrefix = '';
        removePostcodePin();
        statusMessage.textContent = '';
        fitRecords(showCrmMarkers('').bounds);
      }

      fitRecords(showCrmMarkers('').bounds);

      map.on('overlayadd overlayremove', () => {
        if (activePrefix) describePrefix(showCrmMarkers(activePrefix).count);
      });

      clearButton.addEventListener('click', clearPostcodeSearch);

      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        if (searchButton.disabled) return;

        const postcode = normalisePostcode(input.value);
        if (!postcode) {
          clearPostcodeSearch();
          return;
        }

        const isFullPostcode =
          postcode === 'GIR0AA' || /^[A-Z]{1,2}\d[A-Z\d]?\d[A-Z]{2}$/.test(postcode);

        if (!isFullPostcode) {
          if (!/^[A-Z]{1,2}(?:\d[A-Z\d]?)?$/.test(postcode)) {
            statusMessage.textContent = 'Enter a prefix such as TF or a full postcode.';
            return;
          }

          removePostcodePin();
          activePrefix = postcode;
          const { bounds, count } = showCrmMarkers(activePrefix);
          if (bounds.isValid()) fitRecords(bounds);
          describePrefix(count);
          return;
        }

        searchButton.disabled = true;
        clearButton.disabled = true;
        statusMessage.textContent = 'Looking up postcode…';
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 10000);

        try {
          const response = await fetch(
            `${postcodeApi}/${encodeURIComponent(postcode)}`,
            { signal: controller.signal }
          );
          if (response.status === 400 || response.status === 404) {
            statusMessage.textContent = 'Postcode not found. Check it and try again.';
            return;
          }
          if (!response.ok) throw new Error('Lookup failed');

          const { result } = await response.json();
          if (!gbCountries.has(result?.country)) {
            statusMessage.textContent = 'Enter a postcode in England, Scotland or Wales.';
            return;
          }

          const location = [result.latitude, result.longitude];
          if (!location.every(Number.isFinite)) throw new Error('Missing coordinates');

          activePrefix = '';
          showCrmMarkers('');
          removePostcodePin();

          const popup = document.createElement('span');
          popup.textContent = result.postcode;
          map.setView(location, 14);
          postcodePin = L.circleMarker(location, {
            radius: 10,
            color: '#7c3aed'
          }).addTo(map).bindPopup(popup).openPopup();

          statusMessage.textContent = `Showing ${result.postcode}.`;
        } catch {
          statusMessage.textContent = 'The postcode lookup is unavailable. Try again shortly.';
        } finally {
          clearTimeout(timer);
          searchButton.disabled = false;
          clearButton.disabled = false;
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
    show_token_setup(config)

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
                "Search and select records", list(by_id), format_func=labels.get
            )
            selected = [by_id[record_id] for record_id in chosen if record_id in by_id]

        postcodes = sorted({
            record["postcode"] for record in selected if record["postcode"]
        })

        try:
            with st.spinner("Looking up postcodes…"):
                locations, outside_gb = lookup_postcodes(postcodes)

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

            mapped_counts = Counter(pin["module"] for pin in pins)
            for module in modules:
                layer_records = [
                    record for record in selected if record["module"] == module
                ]
                mapped = mapped_counts[module]
                missing = sum(not record["postcode"] for record in layer_records)
                outside = sum(record["postcode"] in outside_gb for record in layer_records)
                unmatched = len(layer_records) - mapped - missing - outside
                missing_label = (
                    "no linked Account postcode" if module == "Deals" else "no postcode"
                )
                st.caption(
                    f"{module} · {mapped:,} mapped · {missing:,} {missing_label} · "
                    f"{outside:,} outside Great Britain · {unmatched:,} postcode not found"
                )
        except (requests.RequestException, ValueError, KeyError, TypeError):
            st.error("The postcode lookup failed. Refresh the page to retry.")

map_data = {
    "pins": pins,
    "modules": modules,
    "countries": GB_COUNTRIES,
    "postcodeApi": POSTCODE_API,
}
html = MAP_HTML.replace("__CRM_DATA__", script_json(map_data), 1)
components.html(html, height=650)
