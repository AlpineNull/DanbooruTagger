DANBOORU TAGGER v0.7.2
=======================

Windows application for anime image tagging using PixAI Tagger v1.0.
Application code developed with ChatGPT (OpenAI).

PIERWSZA INSTALACJA
1. Rozpakuj paczke FULL do wybranego folderu na Windows.
2. Uruchom 1_SETUP.cmd: instalacja bibliotek, pobranie modelu PixAI
   i proba pobrania lokalnej bazy SQLite.
3. Uruchom DanbooruTagger.vbs.
4. Jesli SQLite nie zostala pobrana, uruchom 2_SETUP_TAG_DATABASE.cmd.

AKTUALIZACJA
1. Zamknij aplikacje.
2. Skopiuj zawartosc paczki UPDATE do aktualnego folderu aplikacji,
   potwierdzajac nadpisanie plikow programu.
3. Uruchom DanbooruTagger.vbs. Nie uruchamiaj ponownie 1_SETUP.cmd.

Dane lokalne, model, srodowisko, ustawienia, cache JSON, baza SQLite i klucz API
sa zachowywane. UPDATE nie zawiera katalogu nadrzednego.

TRYBY PRACY
- LOCAL (domyslnie): lokalne sprawdzanie metadanych JSON + SQLite,
  bez automatycznych zapytan do Danbooru dotyczacych tagow.
- HYBRID: opcjonalna kontrola brakujacych lub nieaktualnych danych
  przez oficjalne API Danbooru.
- Odswiez tagi (API): reczne sprawdzenie tagow z bieżącej analizy.
  Po odswiezeniu ponownie kliknij ANALIZUJ.

Przy wlaczonej kontroli blocked artist i podanym Artist URL / nazwie
aplikacja moze wykonac zapytanie API takze w trybie LOCAL.
Przy braku dostepu do API wynik blocked artist pozostaje niezweryfikowany.

DANE OFFLINE
Baza SQLite to snapshot metadanych z 2026-04-08, pobierany osobno.
Brak tagu w tej bazie nie oznacza, ze nie istnieje obecnie na Danbooru.
Istniejacy danbooru_cache.json nie jest usuwany przy aktualizacji.

Licencja oryginalnego kodu aplikacji: MIT (patrz LICENSE).
Model PixAI i zewnetrzna baza danych maja osobne warunki licencyjne.
