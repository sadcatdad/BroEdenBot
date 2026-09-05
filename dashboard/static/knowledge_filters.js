(() => {
  const rows = [...document.querySelectorAll("#knowledge-table tbody tr")];
  const controls = ["knowledge-search", "knowledge-kind", "knowledge-visibility", "knowledge-ai"]
    .map((id) => document.getElementById(id));
  const applyFilters = () => {
    const [search, kind, visibility, ai] = controls.map((control) => control.value.toLowerCase());
    rows.forEach((row) => {
      row.hidden = !(
        (!search || row.dataset.search.includes(search)) &&
        (!kind || row.dataset.kind === kind) &&
        (!visibility || row.dataset.visibility === visibility) &&
        (!ai || row.dataset.ai === ai)
      );
    });
  };
  controls.forEach((control) => control.addEventListener("input", applyFilters));
})();
