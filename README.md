# Sitoo Sync

Hämtar data från Sitoos REST API och sparar den i SQL Server. Tjänsten körs som en Docker-container på QNAP:en och har ett webbgränssnitt för inställningar, status och manuella körningar.

| Data | Tabell | Standardschema |
|---|---|---|
| Butiker | `sitoo.Stores` | Allt, varje timme |
| Lager | `sitoo.Warehouses` | Allt, varje timme |
| Produkter och varianter | `sitoo.Products` | Ändrade var 15:e minut. Borttagna markeras med `IsDeleted = 1` en gång per dygn |
| Lagersaldo | `sitoo.StockItems` | Ändrade per lager, var 15:e minut |
| Ordrar/kvitton | `sitoo.Orders`, `sitoo.OrderItems`, `sitoo.OrderPayments` | Nya varje minut (går att sätta ner till 15 sekunder). Senaste 3 dagarnas ordrar uppdateras var 6:e timme |
| Z-rapporter | `sitoo.ZReports`, `sitoo.ZReportPayments`, `sitoo.ZReportVatGroups`, `sitoo.ZReportProductGroups` | Nya, varje timme |
| Personer | `sitoo.Users` | Allt, var 6:e timme |

Huvudtabellerna har kolumnen `RawJson` med hela posten från Sitoo, lagrad komprimerad. Fält utan egen kolumn läses med funktionen `sitoo.Json`, till exempel `JSON_VALUE(sitoo.Json(RawJson), '$.customerref')`. Orderrader och betalningar finns i orderns `RawJson`. Tider lagras i UTC.

Undvik `SELECT *` på stora tabeller: `RawJson` är den tunga kolumnen. Välj de kolumner du behöver i stället.

Databasen körs med enkel återställningsmodell (SIMPLE), eftersom all data kan hämtas om från Sitoo. Det hindrar loggfilen från att växa obegränsat. Vyerna `sitoo.vLagersaldo` och `sitoo.vOrderrader` visar svensk tid.

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

1. Öppna `http://<QNAP:ens IP-adress>:8095`.
2. Välj ett lösenord för webbgränssnittet.
3. Fyll i uppgifterna för Sitoo och SQL Server. Som server anger du QNAP:ens IP-adress och den port som SQL Server-containern publicerar (oftast 1433).
4. Klicka **Testa anslutning** för Sitoo och för databasen, och sedan **Spara inställningar**.

Synken startar direkt och syns under **Översikt**.

Porten 8095 valdes eftersom QNAP själv använder 8080 och 443. Är 8095 också upptagen ändrar du den första siffran i `ports` i compose-filen, till exempel `"8096:8080"`, och öppnar den porten i stället. Den andra siffran ska alltid vara 8080.

Den första körningen hämtar all historik och kan ta lång tid om ni har många ordrar. Vill du bara ha ordrar från ett visst datum fyller du i **Hämta ordrar från datum** innan du sparar.

---

## Ordrar nästan live

Nya ordrar hämtas varje minut som standard. Intervallet ställs in under **Inställningar > Schema > Nya ordrar (sekunder)**, och lägsta värdet är 15 sekunder. När inget nytt finns kostar en kontroll bara ett API-anrop.

- **Ordrar går först.** Om ett längre jobb pågår, till exempel en full produkthämtning, pausar det mellan sidorna så att nya ordrar hämtas i tid. Sedan fortsätter det där det var.
- **Komplettering vid nya ordrar.** Direkt efter att nya ordrar sparats uppdateras lagersaldot för de artiklar som sålts, i det lager ordern gjordes. Samtidigt hämtas produkter, personal (`userid_staff`) och lager som ordrarna pekar på men som saknas i databasen. Det kan stängas av med **Komplettera vid nya ordrar**. Den första hämtningen av all historik kompletteras inte, eftersom de vanliga jobben ändå hämtar allt.
- **Om något går fel** i kompletteringen sparas ordrarna ändå, och de vanliga jobben för produkter, lagersaldo och personer fångar upp det som missades vid nästa körning.
- **Kunder:** en order innehåller kundens namn, e-post och personnummer direkt, men inget id till en person i Sitoo. Därför hämtas inga kunder automatiskt. Alla personer kommer med via jobbet för personer.
- **Loggen:** orderkontroller som inte hittar något nytt skrivs inte till `sitoo.SyncLog`, så tabellen inte växer i onödan.

### Grafana

Lägg till SQL Server som datakälla i Grafana med inloggningen `sitoosync`, eller en egen inloggning med bara läsrättigheter (`db_datareader`). Använd `OrderDateUtc` som tidskolumn. Tiderna lagras i UTC, och Grafana visar dem i webbläsarens tidszon. Exempel på försäljning per timme:

```sql
SELECT
  DATEADD(HOUR, DATEDIFF(HOUR, 0, OrderDateUtc), 0) AS time,
  SUM(MoneyFinalNet) AS Forsaljning_exkl_moms
FROM sitoo.Orders
WHERE $__timeFilter(OrderDateUtc)
GROUP BY DATEADD(HOUR, DATEDIFF(HOUR, 0, OrderDateUtc), 0)
ORDER BY 1
```

### Färdig dashboard

Filen [`grafana/sitoo-forsaljning-live.json`](grafana/sitoo-forsaljning-live.json) är en färdig dashboard. Importera den under **Dashboards > New > Import** och välj din SQL Server-datakälla. Den jämför försäljningen med **samma veckodag i samma ISO-vecka förra året**, till exempel lördag vecka 28 2026 mot lördag vecka 28 2025, och med **rekorddagen**, den hela dag med högst försäljning genom tiderna. Den visar också ackumulerad försäljning och försäljning per timme, toppsäljare, betalsätt, försäljning per lager, de senaste köpen, de tio bästa dagarna och lågt lagersaldo. Den uppdateras varje minut.

`sitoo.Orders.OrderDateLocal` är orderdatumet i svensk tid. Tabellen `sitoo.DailySales` (och vyn `sitoo.vDagsforsaljning`) har försäljningen per dag i svensk tid och uppdateras automatiskt när ordrar sparas. Den markerar också **readagar** (`ArReadag = 1`): dagar då minst 90 % av de sålda artiklarna har sålts med minst 30 % rabatt (`AndelRea`). Gränserna är standardvärden i proceduren `sitoo.RefreshDailySales`.

## Tidrapport och bokföring

Två sidor i webbgränssnittet ersätter de tidigare Excel-filerna.

**Tidrapport** räknar arbetstid från stämplingar, alltså ordrar med stämplingsartikeln (91000 som standard) som scannas i kassan. *In* är dagens första stämpling, *Ut* den sista och *Timmar* skillnaden. Rapporten filtreras på löneperiod (21:a–20:e som standard) och person. Dagar med en eller ett udda antal stämplingar markeras. Glömda stämplingar läggs till och felaktiga tas bort direkt på sidan (tabellerna `sitoo.TimeStampAdd` och `sitoo.TimeStampRemove`). Rapporten kan exporteras till CSV och skrivas ut.

**Bokföring** bygger ett konteringsförslag per månad, eller för en valfri period, från z-rapporterna. Debet är betalsätten, där kontanter delas på bankningar och kassa. Kredit är försäljning och moms per momssats samt öresavrundning. Momsen räknas som netto × momssats, och skillnaden mot z-rapportens moms läggs på öresavrundningskontot. Under verifikationen visas varje z-rapport. Ett z-rapportdatum kan rättas (`sitoo.ZReportDateFix`), och bankningar av kontanter registreras på sidan (`sitoo.BankDeposit`). Kontona ställs in under **Inställningar > Bokföring**.

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
