# Sitoo Sync

Hämtar data från Sitoos REST API och sparar den i SQL Server. Tjänsten körs som en Docker-container på QNAP:en och har ett webbgränssnitt för inställningar, status och manuella körningar.

| Data | Tabell | Standardschema |
|---|---|---|
| Butiker | `sitoo.Stores` | Allt, varje timme |
| Lager | `sitoo.Warehouses` | Allt, varje timme |
| Produkter och varianter | `sitoo.Products` | Ändrade var 15:e minut. Borttagna markeras med `IsDeleted = 1` en gång per dygn |
| Lagersaldo | `sitoo.StockItems` | Ändrade per lager, var 15:e minut |
| Ordrar/kvitton | `sitoo.Orders`, `sitoo.OrderItems`, `sitoo.OrderPayments` | Nya var 15:e minut. Senaste 3 dagarnas ordrar uppdateras var 6:e timme |
| Z-rapporter | `sitoo.ZReports`, `sitoo.ZReportPayments`, `sitoo.ZReportVatGroups`, `sitoo.ZReportProductGroups` | Nya, varje timme |
| Personer | `sitoo.Users` | Allt, var 6:e timme |

Alla tabeller har kolumnen `RawJson` med hela posten från Sitoo, så fält utan egen kolumn kan läsas med `JSON_VALUE(RawJson, '$.faltnamn')`. Tider lagras i UTC. Vyerna `sitoo.vLagersaldo` och `sitoo.vOrderrader` visar svensk tid.

---

## Installation i Container Station

### 1. Förbered Sitoo och SQL Server

**Sitoo:** i Backoffice under **Inställningar > Sitoo REST API**, skapa en egen API-användare för synken och notera bas-URL, konto-id, API-id och lösenord. Sitoo tillåter bara ett anrop åt gången per nyckel, så dela inte nyckeln med andra integrationer.

**SQL Server:** skapa en inloggning för synken. Kör följande som `sa`, till exempel i SQL Server Management Studio, och byt lösenordet:

```sql
CREATE DATABASE SitooSync;
GO
CREATE LOGIN sitoosync WITH PASSWORD = 'ValjEttStarktLosen123!';
GO
USE SitooSync;
CREATE USER sitoosync FOR LOGIN sitoosync;
ALTER ROLE db_owner ADD MEMBER sitoosync;
GO
```

### 2. Skapa appen

1. Öppna **Container Station** och välj **Applikationer > Skapa**.
2. Ge appen namnet `sitoo-sync` och klistra in innehållet i [`docker-compose.yml`](docker-compose.yml).
3. Klicka **Skapa**. Container Station hämtar imagen och startar containern.

### 3. Gör inställningarna i webbläsaren

1. Öppna `http://<QNAP:ens IP-adress>:8080`.
2. Välj ett lösenord för webbgränssnittet.
3. Fyll i uppgifterna för Sitoo och SQL Server. Som server anger du QNAP:ens IP-adress och den port som SQL Server-containern publicerar (oftast 1433).
4. Klicka **Testa anslutning** för Sitoo och för databasen, och sedan **Spara inställningar**.

Synken startar direkt och syns under **Översikt**.

Den första körningen hämtar all historik och kan ta lång tid om ni har många ordrar. Vill du bara ha ordrar från ett visst datum fyller du i **Hämta ordrar från datum** innan du sparar.

---

## Webbgränssnittet

- **Översikt:** status per jobb, senaste och nästa körning, fel i klartext, antal rader per tabell, **Kör nu**, **Pausa/Starta** och **Hämta om allt** per datatyp.
- **Inställningar:** Sitoo, SQL Server, schema och övrigt. Lösenordsfält som lämnas tomma behåller sitt sparade värde. Här byter du även lösenordet till gränssnittet.
- **Logg:** de senaste körningarna och tjänstens senaste loggrader.

Inställningarna sparas i volymen `sitoo-data` och följer med när containern uppdateras.

**Glömt lösenordet?** Ta bort filen `auth.json` i volymen `sitoo-data` och starta om containern. Då får du välja ett nytt lösenord, och övriga inställningar ligger kvar.

## Uppdatera

Varje gång koden ändras i GitHub bygger GitHub en ny image automatiskt. För att uppdatera går du till appen i Container Station och väljer att hämta den senaste imagen och skapa om containern. Inställningar och data ligger kvar.

## Status i databasen

```sql
SELECT * FROM sitoo.SyncState WHERE Entity LIKE 'job:%';   -- senaste körning och fel per jobb
SELECT TOP 50 * FROM sitoo.SyncLog ORDER BY Id DESC;       -- körningshistorik
```

## Bra att veta

- Sitoos API har ingen ändringsmarkör för ordrar. Ändringar i ordrar äldre än inställningen **Dagar bakåt som ordrar uppdateras** kommer bara med vid **Hämta om allt > Ordrar**.
- Personer som tas bort i Sitoo ligger kvar i `sitoo.Users`.
- Om Sitoo svarar "429 Too Many Requests" väntar tjänsten så länge Sitoo anger. Vid serverfel kan Sitoo låsa nyckeln i upp till 10 minuter, och då väntar tjänsten tills låset släpper.
- Databasen behöver kompatibilitetsnivå 130 eller högre (SQL Server 2016+). En ny databas på SQL Server 2019, 2022 eller 2025 har det automatiskt.
- Fler fält från Sitoo läggs till i fältlistorna i `sitoosync/jobs.py`. De hamnar i `RawJson` utan databasändring.

## Utveckling

```sh
PYTHONPATH=.:tests python -m unittest discover -s tests -v    # tester, kräver bara Python
docker compose -f docker-compose.build.yml up -d --build      # bygg och kör lokalt
```

Kommandorad i containern: `python -m sitoosync --check` testar anslutningarna och `python -m sitoosync --once [jobb]` kör jobb en gång.
