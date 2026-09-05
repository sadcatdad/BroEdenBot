(() => {
      const card = document.querySelector(".brofile-card");
      if (!card) return;
      const colors = {
        accent_color: "--brofile-accent",
        background_color_start: "--brofile-bg-start",
        background_color_end: "--brofile-bg-end"
      };
      Object.entries(colors).forEach(([name, property]) => {
        document.querySelector(`input[type="color"][name="${name}"]`)?.addEventListener("input", event => {
          card.style.setProperty(property, event.target.value);
        });
      });
    })();
