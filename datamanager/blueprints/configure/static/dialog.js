// htmx ignores 4xx responses by default; the modal form uses 422 for validation errors.
document.body.addEventListener("htmx:beforeSwap", (e) => {
  if (e.detail.xhr.status === 422) {
    e.detail.shouldSwap = true;
    e.detail.isError = false;
  }
});

// Open the <dialog> once htmx has swapped it into the modal slot.
document.body.addEventListener("htmx:afterSwap", (e) => {
  if (e.detail.target.id !== "modal-slot") return;
  const dialog = e.detail.target.querySelector("dialog");
  if (dialog && !dialog.open) dialog.showModal();
});
