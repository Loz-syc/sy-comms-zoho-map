import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(page_title="SY Comms CRM Map", layout="wide")
st.title("SY Comms CRM Map")
st.caption("Sample CRM pins. Search for a UK postcode or click a pin to see its details.")

MAP_HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SY Comms CRM Map</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>
    * { box-sizing: border-box; }
    body { margin: 0; font: 16px Arial, sans-serif; }
    form { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
    input, button { font: inherit; padding: 8px 12px; }
    input { width: 10rem; }
    button { cursor: pointer; }
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
  <div id="map" aria-label="Map of sample CRM records"></div>

  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const form = document.querySelector('#postcode-form');
    const input = document.querySelector('#postcode');
    const button = form.querySelector('button');
    const statusMessage = document.querySelector('#status');

    if (typeof L === 'undefined') {
      statusMessage.textContent = 'The map could not load. Refresh the page to try again.';
      button.disabled = true;
    } else {
      initialiseMap();
    }

    function initialiseMap() {
      const map = L.map('map');
      L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
        maxZoom: 19,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
      }).addTo(map);

      const records = [
        ['Accounts', [52.708, -2.754], 'Sample Account · Shrewsbury'],
        ['Leads', [52.807, -2.117], 'Sample Lead · Stafford'],
        ['Deals', [52.486, -1.890], 'Sample Deal · Birmingham']
      ];

      const layers = {};
      for (const [name, location, label] of records) {
        const marker = L.marker(location).bindPopup(label);
        layers[name] = L.layerGroup([marker]).addTo(map);
      }
      L.control.layers({}, layers, { collapsed: false }).addTo(map);
      map.fitBounds(records.map(([, location]) => location), { padding: [45, 45] });

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
          if (response.status === 404 || response.status === 400) {
            statusMessage.textContent = 'Postcode not found. Check it and try again.';
            return;
          }
          if (!response.ok) throw new Error('Postcode lookup failed');

          const { result } = await response.json();
          const location = [result?.latitude, result?.longitude];
          if (!location.every(Number.isFinite)) throw new Error('Missing coordinates');

          const popup = document.createElement('span');
          popup.textContent = result.postcode;
          if (postcodePin) postcodePin.remove();
          postcodePin = L.marker(location).addTo(map).bindPopup(popup);
          map.setView(location, 14);
          postcodePin.openPopup();
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

components.html(MAP_HTML, height=650)
