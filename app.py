import hmac
import json
import math
import re
from collections import Counter
from pathlib import Path

import requests
import streamlit as st
import streamlit.components.v1 as components


MODULES = ("Accounts", "Leads", "Deals")
SCOPES = (
    "ZohoCRM.modules.accounts.READ,"
    "ZohoCRM.modules.leads.READ,"
    "ZohoCRM.modules.deals.READ,"
    "ZohoCRM.coql.READ"
)

DEFAULT_ACCOUNTS_URL = "https://accounts.zoho.eu"
POSTCODE_API = "https://api.postcodes.io/postcodes"
GB_COUNTRIES = {"England", "Scotland", "Wales"}
LOGO_PATH = Path(__file__).with_name("sy-comms-logo.png")

FULL_POSTCODE = re.compile(
    r"(?:GIR0AA|[A-Z]{1,2}[0-9][A-Z0-9]?[0-9][A-Z]{2})"
)
POSTCODE_PREFIX = re.compile(r"[A-Z]{1,2}(?:[0-9][A-Z0-9]?)?")
FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

TOKEN_ERRORS = {
    "invalid_client": "Check the Client ID and Zoho data centre.",
    "invalid_client_secret": "Check the Client Secret for this Self Client.",
    "access_denied": "Check that your Zoho user authorised this client.",
}


def normalise_postcode(value):
    return "".join(str(value or "").upper().split())


def label(value, fallback="Not set"):
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
    credential_field = "code" if grant_type == "authorization_code" else "refresh_token"

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
                "Zoho returned invalid_code. Generate a fresh authorisation "
                "code in the same Self Client and submit it before it expires."
            )
        raise RuntimeError(
            "Zoho rejected the saved refresh token. Use Zoho token setup "
            "to generate a replacement."
        )

    if isinstance(error, str) and error in TOKEN_ERRORS:
        raise RuntimeError(f"Zoho returned {error}. {TOKEN_ERRORS[error]}")

    raise RuntimeError(f"Zoho token request failed (HTTP {response.status_code}).")


def show_token_setup(config):
    setup_password = config.get("setup_password")
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


def validate_search(postcode, account_name):
    if postcode and not (
        FULL_POSTCODE.fullmatch(postcode) or POSTCODE_PREFIX.fullmatch(postcode)
    ):
        raise RuntimeError("Enter a postcode prefix such as TF or a full postcode.")

    if account_name and (
        len(account_name) < 2
        or not any(character.isalnum() for character in account_name)
        or not all(
            character.isalnum() or character in " &.,()-/"
            for character in account_name
        )
    ):
        raise RuntimeError(
            "Enter at least two characters from the account name. "
            "For names with an apostrophe, search using a part without it."
        )


def read_module(session, api_domain, module, fields, conditions):
    if not conditions:
        return []

    columns = ", ".join(dict.fromkeys(("id", *fields)))
    criteria = " and ".join(f"({condition})" for condition in conditions)
    endpoint = f"{api_domain.rstrip('/')}/crm/v8/coql"
    records = {}
    page_size = 2000

    for offset in range(0, 100_000, page_size):
        query = (
            f"select {columns} from {module} where {criteria} "
            f"order by id asc limit {offset}, {page_size}"
        )
        response = session.post(
            endpoint,
            json={"select_query": query},
            timeout=30,
            allow_redirects=False,
        )

        if response.status_code == 204:
            return list(records.values())

        if response.status_code in (401, 403):
            raise RuntimeError(
                f"Zoho denied the {module} search. Check that the saved token "
                "includes ZohoCRM.coql.READ and the module's read scope."
            )

        if response.status_code != 200:
            try:
                error_code = response.json().get("code", "")
            except (ValueError, AttributeError):
                error_code = ""

            if not isinstance(error_code, str) or not re.fullmatch(
                r"[A-Z_]{1,60}", error_code
            ):
                error_code = ""

            detail = f", {error_code}" if error_code else ""
            raise RuntimeError(
                f"Zoho could not search {module} "
                f"(HTTP {response.status_code}{detail})."
            )

        payload = response.json()
        for record in payload["data"]:
            records[record["id"]] = record

        if not payload["info"]["more_records"]:
            return list(records.values())

    raise RuntimeError(f"Too many {module} match. Try a narrower postcode or name.")


@st.cache_data(ttl=600, show_spinner=False, max_entries=64)
def load_map_records(modules, postcode, account_name):
    validate_search(postcode, account_name)
    if not modules or not (postcode or account_name):
        return [], []

    config = st.secrets["zoho"]
    refresh_token = config.get("refresh_token", "").strip()
    if not refresh_token:
        raise RuntimeError("Save a refresh_token under [zoho] in Streamlit Secrets.")

    account_postcode_field = config.get("account_postcode_field", "Post_Code")
    account_type_field = config.get("account_type_field", "Account_Type")
    lead_postcode_field = config.get("lead_postcode_field", "Zip_Code")
    deal_type_field = config.get("deal_type_field", "Deal_Type")

    for field in (
        account_postcode_field,
        account_type_field,
        lead_postcode_field,
        deal_type_field,
    ):
        if not isinstance(field, str) or not FIELD_NAME.fullmatch(field):
            raise RuntimeError("Check the configured field API names in Streamlit Secrets.")

    token = request_zoho_token(config, "refresh_token", refresh_token)
    api_domain = token.get("api_domain")
    if not api_domain:
        raise RuntimeError("Zoho did not return the CRM API domain.")

    fields = {
        "Accounts": ("Account_Name", account_postcode_field, account_type_field),
        "Leads": ("Full_Name", "Company", lead_postcode_field),
        "Deals": ("Deal_Name", "Stage", "Account_Name", deal_type_field),
    }
    postcode_fields = {
        "Accounts": account_postcode_field,
        "Leads": lead_postcode_field,
        "Deals": f"Account_Name.{account_postcode_field}",
    }
    name_fields = {
        "Accounts": "Account_Name",
        "Deals": "Account_Name.Account_Name",
    }

    requested = set(modules)
    if "Deals" in requested:
        requested.add("Accounts")
    if account_name:
        requested.discard("Leads")

    raw = {}
    errors = []

    with requests.Session() as session:
        session.headers["Authorization"] = f"Zoho-oauthtoken {token['access_token']}"

        for module in MODULES:
            if module not in requested:
                continue

            conditions = []
            postcode_field = postcode_fields[module]

            if FULL_POSTCODE.fullmatch(postcode):
                spaced = f"{postcode[:-3]} {postcode[-3:]}"
                conditions.append(f"{postcode_field} in ('{spaced}', '{postcode}')")
            elif postcode:
                conditions.append(f"{postcode_field} like '{postcode}%'")

            if account_name:
                conditions.append(f"{name_fields[module]} like '%{account_name}%'")

            try:
                raw[module] = read_module(
                    session, api_domain, module, fields[module], conditions
                )
            except RuntimeError as error:
                errors.append(str(error))
            except (requests.RequestException, ValueError, KeyError, TypeError):
                errors.append(f"Could not load {module}. Try Refresh Zoho records.")

    records = []
    account_postcodes = {}

    for account in raw.get("Accounts", []):
        account_id = str(account["id"])
        postcode_value = normalise_postcode(account.get(account_postcode_field))
        account_postcodes[account_id] = postcode_value

        if "Accounts" in modules:
            account_type = label(account.get(account_type_field))
            records.append({
                "id": f"Accounts:{account_id}",
                "module": "Accounts",
                "name": label(account.get("Account_Name"), "Unnamed account"),
                "postcode": postcode_value,
                "account_type": account_type,
                "detail": f"Account type: {account_type}",
            })

    for lead in raw.get("Leads", []):
        company = label(lead.get("Company"), "")
        name = label(lead.get("Full_Name"), company or "Unnamed lead")
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
        stage = label(deal.get("Stage"))
        deal_type = label(deal.get(deal_type_field))
        details = [f"Stage: {stage}", f"Deal type: {deal_type}"]

        account_name_label = label(account.get("name"), "")
        if account_name_label:
            details.insert(0, account_name_label)

        records.append({
            "id": f"Deals:{deal['id']}",
            "module": "Deals",
            "name": label(deal.get("Deal_Name"), "Unnamed deal"),
            "postcode": account_postcodes.get(str(account.get("id", "")), ""),
            "stage": stage,
            "deal_type": deal_type,
            "detail": " · ".join(details),
        })

    return sorted(records, key=lambda record: record["name"].casefold()), errors


@st.cache_data(ttl=86400, show_spinner=False, max_entries=128)
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


def choose_filter(records, module, field, title):
    options = sorted(
        {record[field] for record in records if record["module"] == module},
        key=str.casefold,
    )
    return st.multiselect(title, options, placeholder=f"All {title.lower()}")


def filter_records(records, modules):
    account_types = []
    deal_types = []
    deal_stages = []

    if "Accounts" in modules:
        account_types = choose_filter(records, "Accounts", "account_type", "Account types")

    if "Deals" in modules:
        type_column, stage_column = st.columns(2)
        with type_column:
            deal_types = choose_filter(records, "Deals", "deal_type", "Deal types")
        with stage_column:
            deal_stages = choose_filter(records, "Deals", "stage", "Deal stages")

    selected = []
    for record in records:
        if record["module"] == "Accounts":
            if account_types and record["account_type"] not in account_types:
                continue
        elif record["module"] == "Deals":
            if deal_types and record["deal_type"] not in deal_types:
                continue
            if deal_stages and record["stage"] not in deal_stages:
                continue
        selected.append(record)

    mode = st.radio(
        "Show on map",
        ["All matching records", "Select individual records"],
        horizontal=True,
    )
    if mode == "All matching records":
        return selected

    by_id = {record["id"]: record for record in selected}
    labels = {
        record_id: f"{record['name']} ({record['module']})"
        for record_id, record in by_id.items()
    }
    chosen = st.multiselect(
        "Search and select records",
        list(by_id),
        format_func=labels.__getitem__,
    )
    return [by_id[record_id] for record_id in chosen]


def show_counts(records, pins, outside_gb, modules):
    mapped_counts = Counter(pin["module"] for pin in pins)

    for module in modules:
        layer_records = [record for record in records if record["module"] == module]
        mapped = mapped_counts[module]
        missing = sum(not record["postcode"] for record in layer_records)
        outside = sum(record["postcode"] in outside_gb for record in layer_records)
        unmatched = len(layer_records) - mapped - missing - outside
        missing_label = "no linked Account postcode" if module == "Deals" else "no postcode"

        st.caption(
            f"{module} · {len(layer_records):,} selected · {mapped:,} mapped · "
            f"{missing:,} {missing_label} · {outside:,} outside Great Britain · "
            f"{unmatched:,} postcode not found"
        )


MAP_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SY Comms CRM Map</title>
  <link rel="stylesheet"
        href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>
    body { margin: 0; color: #241b38; font: 15px Arial, sans-serif; }
    #map { height: 560px; border: 1px solid #ded7ee; border-radius: 14px; }
    .leaflet-control-layers { border-radius: 10px; }
    .leaflet-popup-content strong { color: #35217a; }
    .layer-dot {
      display: inline-block;
      width: 0.7em;
      height: 0.7em;
      margin-right: 0.4em;
      border-radius: 50%;
    }
    .postcode-marker {
      background: #35217a;
      border: 2px solid white;
      border-radius: 50%;
      box-shadow: 0 1px 5px #0008;
    }
  </style>
</head>
<body>
  <div id="map" aria-label="Map of Zoho CRM records in Great Britain"></div>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const { pins, modules, postcodeLocation } = __CRM_DATA__;
    const mapElement = document.querySelector('#map');

    if (typeof L === 'undefined') {
      mapElement.textContent = 'The map could not load. Refresh the page to retry.';
    } else {
      drawMap();
    }

    function drawMap() {
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
        Accounts: { colour: '#35217a', radius: 9 },
        Leads: { colour: '#15803d', radius: 14 },
        Deals: { colour: '#4292c6', radius: 19 }
      };
      const layers = new Map();
      const overlays = {};

      for (const module of modules) {
        const layer = L.layerGroup().addTo(map);
        layers.set(module, layer);
        const colour = styles[module].colour;
        const name = `<span class="layer-dot" style="background:${colour}"></span>${module}`;
        overlays[name] = layer;
      }

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
          const style = styles[module];
          const location = records[0].location;
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
            fillOpacity: 0.7,
            weight: 3
          }).bindPopup(popup, { maxHeight: 240 }).addTo(layers.get(module));

          bounds.extend(location);
        }
      }

      if (modules.length) {
        L.control.layers({}, overlays, { collapsed: false }).addTo(map);
      }

      if (postcodeLocation) {
        const popup = document.createElement('span');
        popup.textContent = postcodeLocation.postcode;
        const icon = L.divIcon({
          className: 'postcode-marker',
          iconSize: [12, 12],
          iconAnchor: [6, 6]
        });
        L.marker(postcodeLocation.location, { icon })
          .addTo(map).bindPopup(popup);
        bounds.extend(postcodeLocation.location);
      }

      if (bounds.isValid()) {
        map.fitBounds(bounds, { padding: [35, 35], maxZoom: 14 });
      }
    }
  </script>
</body>
</html>
"""


st.set_page_config(
    page_title="SY Comms CRM Map",
    page_icon=str(LOGO_PATH) if LOGO_PATH.is_file() else None,
    layout="wide",
)

if LOGO_PATH.is_file():
    logo_column, title_column = st.columns([1, 7], vertical_alignment="center")
    with logo_column:
        st.image(str(LOGO_PATH), width=135)
    with title_column:
        st.title("CRM Map")
else:
    st.title("SY Comms CRM Map")

try:
    config = st.secrets["zoho"]
except (FileNotFoundError, KeyError):
    config = None
    st.error("Add the [zoho] section in Streamlit → App settings → Secrets.")

if config is not None:
    show_token_setup(config)

modules = st.multiselect("Map layers", MODULES, default=["Accounts"])

with st.form("crm_search"):
    postcode_column, name_column = st.columns(2)
    with postcode_column:
        entered_postcode = st.text_input(
            "Postcode or prefix", placeholder="TF, TF1 or TF1 1AA", max_chars=12
        )
    with name_column:
        entered_name = st.text_input(
            "Account name", placeholder="Robinson", max_chars=100
        )

    search_column, clear_column = st.columns(2)
    with search_column:
        submitted = st.form_submit_button("Search CRM", type="primary")
    with clear_column:
        cleared = st.form_submit_button("Clear results")

if submitted:
    st.session_state["crm_search_values"] = (
        normalise_postcode(entered_postcode),
        entered_name.strip(),
    )
elif cleared:
    st.session_state.pop("crm_search_values", None)

if st.button("Refresh Zoho records"):
    load_map_records.clear()

postcode, account_name = st.session_state.get("crm_search_values", ("", ""))
searched_modules = tuple(
    module for module in modules if not (account_name and module == "Leads")
)
pins = []
postcode_location = None

if postcode or account_name:
    try:
        validate_search(postcode, account_name)
    except RuntimeError as error:
        st.warning(str(error))
    else:
        records = []
        if account_name and "Leads" in modules:
            st.caption(
                "Account name searches cover Accounts and linked Deals. "
                "Clear the name and search by postcode to include Leads."
            )

        if config is not None and searched_modules:
            try:
                with st.spinner("Searching Zoho…"):
                    records, errors = load_map_records(
                        searched_modules, postcode, account_name
                    )
                for error in errors:
                    st.error(error)
            except RuntimeError as error:
                st.error(str(error))
            except KeyError:
                st.error("Check the required [zoho] settings in Streamlit Secrets.")
            except (requests.RequestException, ValueError, TypeError):
                st.error("The Zoho search failed. Try Refresh Zoho records.")

        if records:
            selected = filter_records(records, searched_modules)
        else:
            selected = []
            if searched_modules and config is not None:
                st.caption("No CRM records available to plot for this search.")

        postcodes = {record["postcode"] for record in selected if record["postcode"]}
        exact_postcode = bool(FULL_POSTCODE.fullmatch(postcode))
        if exact_postcode:
            postcodes.add(postcode)

        if postcodes:
            try:
                with st.spinner("Looking up map locations…"):
                    locations, outside_gb = lookup_postcodes(tuple(sorted(postcodes)))

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

                if exact_postcode:
                    postcode_location = locations.get(postcode)
                    if postcode in outside_gb:
                        st.info("That postcode is outside England, Scotland and Wales.")
                    elif not postcode_location:
                        st.info("That postcode could not be located.")

                if records:
                    show_counts(selected, pins, outside_gb, searched_modules)
            except (requests.RequestException, ValueError, KeyError, TypeError):
                st.error("The postcode lookup failed. Search again to retry.")
        elif records:
            show_counts(selected, pins, set(), searched_modules)
else:
    st.caption("Enter a postcode, prefix or account name to load records from Zoho.")

map_data = {
    "pins": pins,
    "modules": searched_modules,
    "postcodeLocation": postcode_location,
}
components.html(
    MAP_HTML.replace("__CRM_DATA__", script_json(map_data), 1),
    height=570,
)
