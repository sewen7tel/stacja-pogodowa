# Stacja pogodowa – archiwum i dashboard

Co godzinę GitHub Actions:
1. pobiera nowe pomiary z chmury Ecowitt (stacja GoGEN ME3900) i dane dobowe IMGW,
2. dopisuje je do archiwum `dane/archiwum.sqlite` (gałąź `dane`),
3. publikuje dashboard na GitHub Pages.

Pliki w tym repozytorium:
- `stacja_pogodowa.py`, `szablon_dashboard.html` – skrypt i wygląd strony,
- `weathercloud/` – eksporty CSV z poprzedniej stacji VEVOR (Weathercloud),
- `ilość opadów.xlsx` – miesięczne sumy z deszczomierza ręcznego. Po dopisaniu miesiąca wgraj nową wersję pliku (Add file → Upload files). Strona odświeży się po kilku minutach.

Klucze API są w sekretach repozytorium (Settings → Secrets and variables → Actions):
`ECOWITT_APP_KEY`, `ECOWITT_API_KEY`, `ECOWITT_MAC`. Nie ma ich w żadnym pliku.

Ręczne odświeżenie: zakładka **Actions**, potem **Stacja pogodowa**, potem **Run workflow**.
