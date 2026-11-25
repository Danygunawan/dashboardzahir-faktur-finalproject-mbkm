// Dibaca sebelum halaman tampil, supaya tema tersimpan tidak berkedip saat refresh.
(() => {
  let theme = 'light';
  try { if (localStorage.getItem('faktur-lokal-tema') === 'dark') theme = 'dark'; } catch (_) {}
  document.documentElement.dataset.theme = theme;
})();
