DANBOORU TAGGER v0.5.1
====================

AKTUALIZACJA Z v0.5.0 / v0.4.x
1. Zamknij Danbooru Tagger.
2. Rozpakuj paczke UPDATE do obecnego folderu programu i wybierz zastapienie plikow.
3. Uruchom DanbooruTagger.vbs jak dotad.
4. Nie uruchamiaj ponownie 1_SETUP.cmd. Model i .venv zostaja bez zmian.

NAJWAZNIEJSZE ZMIANY v0.5.1

1. CHARACTER / COPYRIGHT
- v0.5.0 uzywalo heurystyki suffixu character taga i moglo zglaszac poprawne pary jako
  crossover, np. green_heart_(neptunia) + choujigen_game_neptune.
- v0.5.1 pyta read-only related_tag Danbooru o copyrighty zwiazane z dana postacia.
- Warning [COPYRIGHT] pojawia sie tylko, gdy istnieje potwierdzona relacja i zaden z
  potwierdzonych copyrightow nie wystepuje w aktualnym wyniku.
- Gdy API/cache nie daje pewnej relacji, program nie zgaduje.

2. KONFLIKTY ROZMIARU PIERSI
- Przy pojedynczej postaci POMOC ostrzega o jednoczesnych tagach z grupy:
  flat_chest, small_breasts, medium_breasts, large_breasts, huge_breasts.
- Przy wielu postaciach konflikt jest tlumiony tak samo jak kolory oczu i dlugosci wlosow.

3. SUGESTIE TUZ PONIZEJ LOWEST
- Opcja domyslnie wlaczona w Ustawieniach: "Pokazuj trafne tagi tuz ponizej progu LOWEST".
- Model nadal wykonuje tylko jeden przebieg GPU.
- Dla domyslnego LOWEST 0.17 model pobiera kandydatow do 0.10, ale do bucketow trafiaja
  nadal tylko tagi >= 0.17.
- Maksymalnie 6 istniejacych, nie-deprecated kandydatow pojawia sie w POMOC jako:
  [NISKI PROG] Do sprawdzenia: tag1 tag2 ...
- Sugestie nie sa automatycznie dopisywane do HIGH/MEDIUM/LOW ani Kopiuj ALL.
- Funkcja jest przeznaczona m.in. do missow typu gradient_background bez obnizania
  glownego progu LOWEST.

4. CACHE
- Cache schema: v5.
- Dodano character_copyrights.
- Cache v2/v3/v4 migruje sie automatycznie. Nie trzeba usuwac danbooru_cache.json.
- Tagi istniejace nadal maja TTL 90 dni, nieistniejace 30 dni.

POZOSTALE FUNKCJE v0.5.0 ZOSTAJA
- group-aware konflikty,
- warningi nieistniejacych tagow bez automatycznego usuwania,
- character coverage,
- wspolwystepujace postacie,
- greyscale sanity check,
- translation_request,
- aliasy/deprecated/implications,
- blocked artist precheck,
- automatyczne zwalnianie VRAM,
- single-instance guard,
- Kopiuj ALL z pustymi liniami pomiedzy bucketami.

DOMYSLNE PROGI
High:   >= 0.75
Medium: >= 0.45
Lowest: >= 0.17

PRYWATNOSC
- Obraz jest analizowany lokalnie.
- Danbooru dostaje tylko nazwy tagow oraz Artist URL / nazwe przy checku artysty.

PIERWSZA INSTALACJA
Na nowym komputerze uzyj FULL i uruchom 1_SETUP.cmd jeden raz.
