(() => {
    const input = document.querySelector("[data-brofile-search]");
    const cards = [...document.querySelectorAll("[data-brofile-grid] > a")];
    const empty = document.querySelector("[data-brofile-empty]");
    input?.addEventListener("input", () => {
      const query = input.value.trim().toLocaleLowerCase();
      let visible = 0;
      cards.forEach(card => {
        const matches = !query || (card.dataset.search || "").toLocaleLowerCase().includes(query);
        card.hidden = !matches;
        if (matches) visible += 1;
      });
      if (empty) empty.hidden = visible !== 0;
    });
  })();
