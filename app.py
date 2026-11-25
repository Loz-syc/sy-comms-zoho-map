      const input = document.querySelector('#postcode');
      const button = form.querySelector('button');
      let postcodePin;

      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        if (button.disabled) return;
        const postcode = input.value.trim().toUpperCase().replace(/\\s+/g, '');
        if (!postcode) {
          status.textContent = 'Enter a UK postcode.';
          return;
        }

        button.disabled = true;
        status.textContent = 'Looking up postcode…';
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 10000);
        try {
          const response = await fetch(
            'https://api.postcodes.io/postcodes/' + encodeURIComponent(postcode),
            { signal: controller.signal }
          );
          if (response.status === 404 || response.status === 400) {
            status.textContent = 'Postcode not found. Check it and try again.';
            return;
          }
          if (!response.ok) throw new Error('Postcode lookup failed');

          const { result } = await response.json();
          const location = [result.latitude, result.longitude];
          if (!location.every(Number.isFinite)) throw new Error('Missing coordinates');

          const popup = document.createElement('span');
          popup.textContent = result.postcode;
          if (postcodePin) postcodePin.remove();
          postcodePin = L.marker(location).addTo(map).bindPopup(popup);
          map.setView(location, 14);
          postcodePin.openPopup();
          status.textContent = 'Showing ' + result.postcode;
        } catch {
          status.textContent = 'The postcode lookup is unavailable. Try again shortly.';
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

st.iframe(MAP_HTML, height=650)
