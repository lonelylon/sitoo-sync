-- Sitoo Sync – databasschema
-- Körs automatiskt vid varje start. Allt är idempotent: tabeller skapas bara
-- om de saknas och procedurer/vyer skrivs över med senaste version.
-- Alla tider lagras i UTC (kolumner som slutar på Utc). Vyerna visar svensk tid.

IF SCHEMA_ID(N'sitoo') IS NULL EXEC(N'CREATE SCHEMA sitoo');
GO

------------------------------------------------------------------------------
-- Hjälpfunktion: Unix-sekunder -> DATETIME2 (UTC)
------------------------------------------------------------------------------
CREATE OR ALTER FUNCTION sitoo.FromUnix (@s BIGINT)
RETURNS DATETIME2(0)
AS
BEGIN
    RETURN CASE
        WHEN @s IS NULL OR @s <= 0 THEN NULL
        ELSE DATEADD(SECOND, CAST(@s % 86400 AS INT),
                     DATEADD(DAY, CAST(@s / 86400 AS INT), CAST('19700101' AS DATETIME2(0))))
    END;
END;
GO

------------------------------------------------------------------------------
-- Synkstatus och logg
------------------------------------------------------------------------------
IF OBJECT_ID(N'sitoo.SyncState', N'U') IS NULL
CREATE TABLE sitoo.SyncState (
    Entity          NVARCHAR(200)  NOT NULL CONSTRAINT PK_SyncState PRIMARY KEY,
    LastValue       BIGINT         NULL,
    LastAttemptUtc  DATETIME2(0)   NULL,
    LastSuccessUtc  DATETIME2(0)   NULL,
    LastError       NVARCHAR(4000) NULL
);
GO

IF OBJECT_ID(N'sitoo.SyncLog', N'U') IS NULL
CREATE TABLE sitoo.SyncLog (
    Id           BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_SyncLog PRIMARY KEY,
    Job          NVARCHAR(100)  NOT NULL,
    StartedUtc   DATETIME2(0)   NOT NULL,
    FinishedUtc  DATETIME2(0)   NOT NULL,
    Status       NVARCHAR(20)   NOT NULL,
    RowsAffected INT            NULL,
    Message      NVARCHAR(4000) NULL
);
GO

------------------------------------------------------------------------------
-- Butiker
------------------------------------------------------------------------------
IF OBJECT_ID(N'sitoo.Stores', N'U') IS NULL
CREATE TABLE sitoo.Stores (
    SiteId          INT            NOT NULL,
    StoreId         INT            NOT NULL,
    EshopId         INT            NULL,
    StoreType       INT            NULL,
    Name            NVARCHAR(255)  NULL,
    ExternalId      NVARCHAR(255)  NULL,
    ExternalGroupId NVARCHAR(255)  NULL,
    Phone           NVARCHAR(100)  NULL,
    Email           NVARCHAR(320)  NULL,
    Address         NVARCHAR(255)  NULL,
    Address2        NVARCHAR(255)  NULL,
    Zip             NVARCHAR(50)   NULL,
    City            NVARCHAR(255)  NULL,
    State           NVARCHAR(100)  NULL,
    CountryId       NVARCHAR(10)   NULL,
    Latitude        NVARCHAR(30)   NULL,
    Longitude       NVARCHAR(30)   NULL,
    IsDeleted       BIT            NOT NULL CONSTRAINT DF_Stores_IsDeleted DEFAULT 0,
    RawJson         VARBINARY(MAX)  NULL,
    SyncedUtc       DATETIME2(0)   NOT NULL,
    CONSTRAINT PK_Stores PRIMARY KEY (SiteId, StoreId)
);
GO

------------------------------------------------------------------------------
-- Leverantörer (Sitoos tillverkare, manufacturers)
------------------------------------------------------------------------------
IF OBJECT_ID(N'sitoo.Manufacturers', N'U') IS NULL
CREATE TABLE sitoo.Manufacturers (
    SiteId         INT           NOT NULL,
    ManufacturerId INT           NOT NULL,
    Name           NVARCHAR(255) NULL,
    IsDeleted      BIT           NOT NULL CONSTRAINT DF_Manufacturers_IsDeleted DEFAULT 0,
    RawJson        VARBINARY(MAX) NULL,
    SyncedUtc      DATETIME2(0)  NOT NULL,
    CONSTRAINT PK_Manufacturers PRIMARY KEY (SiteId, ManufacturerId)
);
GO

------------------------------------------------------------------------------
-- Lager
------------------------------------------------------------------------------
IF OBJECT_ID(N'sitoo.Warehouses', N'U') IS NULL
CREATE TABLE sitoo.Warehouses (
    SiteId        INT           NOT NULL,
    WarehouseId   INT           NOT NULL,
    WarehouseType INT           NULL,   -- 10 = FIFO, 20 = Virtual
    UseType       INT           NULL,   -- 10 Front, 20 Back, 30 Demo, 40 Reclaim, 50 External
    StoreId       INT           NULL,
    Sellable      BIT           NULL,
    CurrencyCode  NVARCHAR(3)   NULL,
    Name          NVARCHAR(255) NULL,
    ExternalId    NVARCHAR(255) NULL,
    Address       NVARCHAR(255) NULL,
    Zip           NVARCHAR(50)  NULL,
    City          NVARCHAR(255) NULL,
    CountryId     NVARCHAR(10)  NULL,
    IsDeleted     BIT           NOT NULL CONSTRAINT DF_Warehouses_IsDeleted DEFAULT 0,
    RawJson       VARBINARY(MAX) NULL,
    SyncedUtc     DATETIME2(0)  NOT NULL,
    CONSTRAINT PK_Warehouses PRIMARY KEY (SiteId, WarehouseId)
);
GO

------------------------------------------------------------------------------
-- Produkter (inklusive varianter: VariantParentId pekar på huvudprodukten)
------------------------------------------------------------------------------
IF OBJECT_ID(N'sitoo.Products', N'U') IS NULL
CREATE TABLE sitoo.Products (
    SiteId              INT           NOT NULL,
    ProductId           INT           NOT NULL,
    Sku                 NVARCHAR(255) NULL,
    Title               NVARCHAR(500) NULL,
    VariantParentId     INT           NULL,
    Barcode             NVARCHAR(255) NULL,
    Active              BIT           NULL,
    ActivePos           BIT           NULL,
    VatId               INT           NULL,
    VatValue            DECIMAL(9,4)  NULL,
    ManufacturerId      INT           NULL,
    DefaultCategoryId   INT           NULL,
    MoneyPrice          DECIMAL(19,2) NULL,
    MoneyPriceOrg       DECIMAL(19,2) NULL,
    MoneyPriceIn        DECIMAL(19,2) NULL,
    MoneyFinalPrice     DECIMAL(19,2) NULL,
    StockCountEnable    BIT           NULL,
    UnitLabel           NVARCHAR(50)  NULL,
    SkuManufacturer     NVARCHAR(255) NULL,
    DateCreatedUtc      DATETIME2(0)  NULL,
    DateModifiedUtc     DATETIME2(0)  NULL,
    IsDeleted           BIT           NOT NULL CONSTRAINT DF_Products_IsDeleted DEFAULT 0,
    DeletedUtc          DATETIME2(0)  NULL,
    RawJson             VARBINARY(MAX) NULL,
    SyncedUtc           DATETIME2(0)  NOT NULL,
    CONSTRAINT PK_Products PRIMARY KEY (SiteId, ProductId)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_Products_Sku' AND object_id = OBJECT_ID(N'sitoo.Products'))
    CREATE INDEX IX_Products_Sku ON sitoo.Products (SiteId, Sku);
GO

------------------------------------------------------------------------------
-- Lagersaldo per lager och artikel
------------------------------------------------------------------------------
IF OBJECT_ID(N'sitoo.StockItems', N'U') IS NULL
CREATE TABLE sitoo.StockItems (
    SiteId                 INT           NOT NULL,
    WarehouseId            INT           NOT NULL,
    Sku                    NVARCHAR(255) NOT NULL,
    WarehouseItemId        BIGINT        NULL,
    BinLocation            NVARCHAR(100) NULL,
    DecimalThreshold       DECIMAL(19,3) NULL,
    DecimalTotal           DECIMAL(19,3) NULL,
    DecimalReserved        DECIMAL(19,3) NULL,
    DecimalAvailable       DECIMAL(19,3) NULL,
    MoneyTotal             DECIMAL(19,2) NULL,
    DateLastModifiedUtc    DATETIME2(0)  NULL,
    DateLastStocktakingUtc DATETIME2(0)  NULL,
    RawJson                VARBINARY(MAX) NULL,
    SyncedUtc              DATETIME2(0)  NOT NULL,
    CONSTRAINT PK_StockItems PRIMARY KEY (SiteId, WarehouseId, Sku)
);
GO

------------------------------------------------------------------------------
-- Ordrar / kvitton
------------------------------------------------------------------------------
IF OBJECT_ID(N'sitoo.Orders', N'U') IS NULL
CREATE TABLE sitoo.Orders (
    OrderId                  BIGINT        NOT NULL CONSTRAINT PK_Orders PRIMARY KEY,
    EshopId                  INT           NULL,
    ExternalId               NVARCHAR(255) NULL,
    CreditOrderId            BIGINT        NULL,
    CartId                   NVARCHAR(64)  NULL,
    OrderDateUtc             DATETIME2(0)  NULL,
    OrderDateLocal           DATETIME2(0)  NULL,   -- svensk tid, för dagar/veckor i rapporter
    OrderStateId             INT           NULL,
    PaymentStateId           INT           NULL,
    OrderTypeId              INT           NULL,
    RegisterId               NVARCHAR(100) NULL,
    WarehouseId              INT           NULL,
    UserIdStaff              NVARCHAR(64)  NULL,
    CurrencyCode             NVARCHAR(3)   NULL,
    Email                    NVARCHAR(320) NULL,
    NameFirst                NVARCHAR(255) NULL,
    NameLast                 NVARCHAR(255) NULL,
    PersonalId               NVARCHAR(50)  NULL,
    Company                  NVARCHAR(255) NULL,
    Phone                    NVARCHAR(100) NULL,
    CustomerRef              NVARCHAR(255) NULL,
    MoneyTotalNet            DECIMAL(19,2) NULL,
    MoneyTotalVat            DECIMAL(19,2) NULL,
    MoneyFinalNet            DECIMAL(19,2) NULL,
    MoneyFinalVat            DECIMAL(19,2) NULL,
    MoneyFinalSalesTax       DECIMAL(19,2) NULL,
    MoneyTotalGrossRoundoff  DECIMAL(19,2) NULL,
    MoneyTotalGrossAll       DECIMAL(19,2) NULL,
    Comment                  NVARCHAR(MAX) NULL,
    RawJson                  VARBINARY(MAX) NULL,
    SyncedUtc                DATETIME2(0)  NOT NULL
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_Orders_OrderDate' AND object_id = OBJECT_ID(N'sitoo.Orders'))
    CREATE INDEX IX_Orders_OrderDate ON sitoo.Orders (OrderDateUtc) INCLUDE (EshopId, WarehouseId, OrderTypeId);
GO

IF OBJECT_ID(N'sitoo.OrderItems', N'U') IS NULL
CREATE TABLE sitoo.OrderItems (
    OrderId               BIGINT        NOT NULL,
    LineNumber            INT           NOT NULL,
    OrderItemId           BIGINT        NULL,
    OrderItemType         INT           NULL,
    ProductId             INT           NULL,
    Sku                   NVARCHAR(255) NULL,
    ProductName           NVARCHAR(500) NULL,
    Quantity              DECIMAL(19,3) NULL,
    DecimalQuantityTotal  DECIMAL(19,3) NULL,
    UnitLabel             NVARCHAR(50)  NULL,
    VatValue              DECIMAL(9,4)  NULL,
    MoneyNetPricePerUnit  DECIMAL(19,2) NULL,
    MoneyPriceOrg         DECIMAL(19,2) NULL,
    MoneyOriginalPrice    DECIMAL(19,2) NULL,
    MoneyDiscountedPrice  DECIMAL(19,2) NULL,
    MoneyDiscount         DECIMAL(19,2) NULL,
    MoneyItemTotalNet     DECIMAL(19,2) NULL,
    MoneyItemTotalVat     DECIMAL(19,2) NULL,
    VoucherId             INT           NULL,
    VoucherCode           NVARCHAR(255) NULL,
    VoucherName           NVARCHAR(255) NULL,
    CONSTRAINT PK_OrderItems PRIMARY KEY (OrderId, LineNumber)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_OrderItems_Sku' AND object_id = OBJECT_ID(N'sitoo.OrderItems'))
    CREATE INDEX IX_OrderItems_Sku ON sitoo.OrderItems (Sku);
GO

IF OBJECT_ID(N'sitoo.OrderPayments', N'U') IS NULL
CREATE TABLE sitoo.OrderPayments (
    OrderId     BIGINT        NOT NULL,
    LineNumber  INT           NOT NULL,
    Name        NVARCHAR(255) NULL,
    MoneyAmount DECIMAL(19,2) NULL,
    CONSTRAINT PK_OrderPayments PRIMARY KEY (OrderId, LineNumber)
);
GO

------------------------------------------------------------------------------
-- Z-rapporter (per kassa) med betalningar, momsgrupper och varugrupper
------------------------------------------------------------------------------
IF OBJECT_ID(N'sitoo.ZReports', N'U') IS NULL
CREATE TABLE sitoo.ZReports (
    RegisterId              NVARCHAR(100) NOT NULL,
    ZReportId               INT           NOT NULL,
    EshopId                 INT           NULL,
    StoreExternalId         NVARCHAR(255) NULL,
    StoreName               NVARCHAR(255) NULL,
    RegisterNumber          INT           NULL,
    Company                 NVARCHAR(255) NULL,
    Staff                   NVARCHAR(255) NULL,
    StaffUserId             NVARCHAR(64)  NULL,
    CurrencyCode            NVARCHAR(3)   NULL,
    DateOpenedUtc           DATETIME2(0)  NULL,
    DateCreatedUtc          DATETIME2(0)  NULL,
    DateAddedUtc            DATETIME2(0)  NULL,
    MoneySalesTotal         DECIMAL(19,2) NULL,
    MoneySalesTotalNet      DECIMAL(19,2) NULL,
    NumSales                INT           NULL,
    NumSalesItems           INT           NULL,
    MoneyRefundTotal        DECIMAL(19,2) NULL,
    MoneyRefundTotalNet     DECIMAL(19,2) NULL,
    NumRefund               INT           NULL,
    NumRefundItems          INT           NULL,
    MoneyDiscount           DECIMAL(19,2) NULL,
    MoneyRoundOff           DECIMAL(19,2) NULL,
    MoneySummaryTotal       DECIMAL(19,2) NULL,
    MoneyCashExpected       DECIMAL(19,2) NULL,
    MoneyCashCounted        DECIMAL(19,2) NULL,
    MoneyCashDiff           DECIMAL(19,2) NULL,
    NumReceipts             INT           NULL,
    MoneyGrandTotalSales    DECIMAL(19,2) NULL,
    MoneyGrandTotalRefund   DECIMAL(19,2) NULL,
    MoneyGrandTotalNet      DECIMAL(19,2) NULL,
    RawJson                 VARBINARY(MAX) NULL,
    SyncedUtc               DATETIME2(0)  NOT NULL,
    CONSTRAINT PK_ZReports PRIMARY KEY (RegisterId, ZReportId)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_ZReports_DateCreated' AND object_id = OBJECT_ID(N'sitoo.ZReports'))
    CREATE INDEX IX_ZReports_DateCreated ON sitoo.ZReports (DateCreatedUtc);
GO

IF OBJECT_ID(N'sitoo.ZReportPayments', N'U') IS NULL
CREATE TABLE sitoo.ZReportPayments (
    RegisterId  NVARCHAR(100) NOT NULL,
    ZReportId   INT           NOT NULL,
    Kind        NVARCHAR(30)  NOT NULL,   -- sales / refund
    LineNumber  INT           NOT NULL,
    Name        NVARCHAR(255) NULL,
    MoneyTotal  DECIMAL(19,2) NULL,
    CONSTRAINT PK_ZReportPayments PRIMARY KEY (RegisterId, ZReportId, Kind, LineNumber)
);
GO

IF OBJECT_ID(N'sitoo.ZReportVatGroups', N'U') IS NULL
CREATE TABLE sitoo.ZReportVatGroups (
    RegisterId     NVARCHAR(100) NOT NULL,
    ZReportId      INT           NOT NULL,
    Kind           NVARCHAR(30)  NOT NULL,   -- sales / salesnotdelivered / refund
    LineNumber     INT           NOT NULL,
    VatValue       DECIMAL(9,4)  NULL,
    MoneyTotal     DECIMAL(19,2) NULL,
    MoneyTotalNet  DECIMAL(19,2) NULL,
    MoneyTotalVat  DECIMAL(19,2) NULL,
    CONSTRAINT PK_ZReportVatGroups PRIMARY KEY (RegisterId, ZReportId, Kind, LineNumber)
);
GO

IF OBJECT_ID(N'sitoo.ZReportProductGroups', N'U') IS NULL
CREATE TABLE sitoo.ZReportProductGroups (
    RegisterId     NVARCHAR(100) NOT NULL,
    ZReportId      INT           NOT NULL,
    Kind           NVARCHAR(30)  NOT NULL,   -- sales / salesnotdelivered / refund
    LineNumber     INT           NOT NULL,
    Name           NVARCHAR(255) NULL,
    VatValue       DECIMAL(9,4)  NULL,
    NumTotal       DECIMAL(19,3) NULL,
    MoneyTotal     DECIMAL(19,2) NULL,
    MoneyTotalNet  DECIMAL(19,2) NULL,
    MoneyTotalVat  DECIMAL(19,2) NULL,
    CONSTRAINT PK_ZReportProductGroups PRIMARY KEY (RegisterId, ZReportId, Kind, LineNumber)
);
GO

------------------------------------------------------------------------------
-- Personer (användare i Sitoo: personal och kunder, delas mellan alla siter)
------------------------------------------------------------------------------
IF OBJECT_ID(N'sitoo.Users', N'U') IS NULL
CREATE TABLE sitoo.Users (
    UserId          NVARCHAR(64)  NOT NULL CONSTRAINT PK_Users PRIMARY KEY,
    Email           NVARCHAR(320) NULL,
    NameFirst       NVARCHAR(255) NULL,
    NameLast        NVARCHAR(255) NULL,
    PersonalId      NVARCHAR(50)  NULL,
    ExternalId      NVARCHAR(255) NULL,
    CustomerNumber  NVARCHAR(100) NULL,
    Company         NVARCHAR(255) NULL,
    Department      NVARCHAR(255) NULL,
    CompanyId       NVARCHAR(100) NULL,
    Address         NVARCHAR(255) NULL,
    Address2        NVARCHAR(255) NULL,
    Zip             NVARCHAR(50)  NULL,
    City            NVARCHAR(255) NULL,
    State           NVARCHAR(100) NULL,
    CountryId       NVARCHAR(10)  NULL,
    Phone           NVARCHAR(100) NULL,
    Mobile          NVARCHAR(100) NULL,
    Role            INT           NULL,   -- 10 Admin, 20 Staff, 30 StoreManager, 40 StoreStaff, 50 Viewer, NULL = ingen roll
    Active          BIT           NULL,
    Notes           NVARCHAR(MAX) NULL,
    DateCreatedUtc  DATETIME2(0)  NULL,
    DateModifiedUtc DATETIME2(0)  NULL,
    RawJson         VARBINARY(MAX) NULL,
    SyncedUtc       DATETIME2(0)  NOT NULL
);
GO

------------------------------------------------------------------------------
-- Enkel återställningsmodell: all data kan hämtas om från Sitoo, så loggfilen
-- behöver inte sparas för punktåterställning. Hindrar att loggen växer utan gräns.
------------------------------------------------------------------------------
BEGIN TRY
    IF (SELECT recovery_model FROM sys.databases WHERE database_id = DB_ID()) <> 3
        ALTER DATABASE CURRENT SET RECOVERY SIMPLE;
END TRY
BEGIN CATCH
    PRINT 'Kunde inte sätta SIMPLE recovery: ' + ERROR_MESSAGE();
END CATCH;
GO

------------------------------------------------------------------------------
-- RawJson lagras komprimerad (COMPRESS). Läs den med sitoo.Json(RawJson),
-- t.ex. JSON_VALUE(sitoo.Json(RawJson), '$.customerref').
------------------------------------------------------------------------------
CREATE OR ALTER FUNCTION sitoo.Json (@raw VARBINARY(MAX))
RETURNS NVARCHAR(MAX)
WITH SCHEMABINDING
AS
BEGIN
    RETURN CAST(DECOMPRESS(@raw) AS NVARCHAR(MAX));
END;
GO

-- Gör om en äldre RawJson-kolumn (NVARCHAR) till komprimerad VARBINARY.
-- Kan avbrytas: nästa start gör om eller slutför det som återstår.
CREATE OR ALTER PROCEDURE sitoo.MigrateRawJson
    @Table SYSNAME
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @obj INT = OBJECT_ID(N'sitoo.' + @Table, N'U');
    IF @obj IS NULL RETURN;
    DECLARE @old NVARCHAR(400) = N'sitoo.' + @Table + N'.RawJsonZ';
    -- Avbrutet efter DROP COLUMN men före namnbytet: slutför namnbytet.
    IF COL_LENGTH(N'sitoo.' + @Table, N'RawJson') IS NULL AND COL_LENGTH(N'sitoo.' + @Table, N'RawJsonZ') IS NOT NULL
    BEGIN
        EXEC sp_rename @old, N'RawJson', N'COLUMN';
        RETURN;
    END;
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = @obj AND name = N'RawJson'
                   AND system_type_id = TYPE_ID(N'nvarchar'))
        RETURN;

    DECLARE @t NVARCHAR(300) = N'sitoo.' + QUOTENAME(@Table);
    DECLARE @sql NVARCHAR(MAX);

    IF COL_LENGTH(N'sitoo.' + @Table, N'RawJsonZ') IS NULL
    BEGIN
        SET @sql = N'ALTER TABLE ' + @t + N' ADD RawJsonZ VARBINARY(MAX) NULL;';
        EXEC (@sql);
    END;

    SET @sql = N'UPDATE ' + @t + N' SET RawJsonZ = COMPRESS(RawJson) WHERE RawJsonZ IS NULL AND RawJson IS NOT NULL;';
    EXEC (@sql);

    SET @sql = N'ALTER TABLE ' + @t + N' DROP COLUMN RawJson;';
    EXEC (@sql);
    EXEC sp_rename @old, N'RawJson', N'COLUMN';
    SET @sql = N'ALTER TABLE ' + @t + N' REBUILD;';
    EXEC (@sql);
    PRINT 'RawJson komprimerad i ' + @t;
END;
GO

-- Tar bort RawJson i tabeller där samma data redan finns i föräldraraden.
CREATE OR ALTER PROCEDURE sitoo.DropRawJson
    @Table SYSNAME
AS
BEGIN
    SET NOCOUNT ON;
    IF COL_LENGTH(N'sitoo.' + @Table, N'RawJson') IS NULL RETURN;
    DECLARE @t NVARCHAR(300) = N'sitoo.' + QUOTENAME(@Table);
    DECLARE @sql NVARCHAR(MAX) = N'ALTER TABLE ' + @t + N' DROP COLUMN RawJson; ALTER TABLE ' + @t + N' REBUILD;';
    EXEC (@sql);
    PRINT 'RawJson borttagen i ' + @t;
END;
GO

EXEC sitoo.DropRawJson N'OrderItems';
GO
EXEC sitoo.DropRawJson N'OrderPayments';
GO
EXEC sitoo.DropRawJson N'ZReportPayments';
GO
EXEC sitoo.MigrateRawJson N'Stores';
GO
EXEC sitoo.MigrateRawJson N'Warehouses';
GO
EXEC sitoo.MigrateRawJson N'Users';
GO
EXEC sitoo.MigrateRawJson N'ZReports';
GO
EXEC sitoo.MigrateRawJson N'Products';
GO
EXEC sitoo.MigrateRawJson N'StockItems';
GO
EXEC sitoo.MigrateRawJson N'Orders';
GO

------------------------------------------------------------------------------
-- Orderdatum i svensk tid (OrderDateLocal). AT TIME ZONE kan inte indexeras som
-- beräknad kolumn, så värdet sparas vid varje MERGE och fylls i för äldre rader.
------------------------------------------------------------------------------
IF COL_LENGTH(N'sitoo.Orders', N'OrderDateLocal') IS NULL
    ALTER TABLE sitoo.Orders ADD OrderDateLocal DATETIME2(0) NULL;
GO
UPDATE sitoo.Orders
SET OrderDateLocal = CAST(OrderDateUtc AT TIME ZONE 'UTC' AT TIME ZONE 'W. Europe Standard Time' AS DATETIME2(0))
WHERE OrderDateLocal IS NULL AND OrderDateUtc IS NOT NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_Orders_OrderDateLocal' AND object_id = OBJECT_ID(N'sitoo.Orders'))
    CREATE INDEX IX_Orders_OrderDateLocal ON sitoo.Orders (OrderDateLocal)
        INCLUDE (MoneyFinalNet, MoneyTotalGrossAll, WarehouseId, EshopId);
GO

------------------------------------------------------------------------------
-- Försäljning per dag (svensk tid), förberäknad så att rapporter som letar efter
-- rekorddagar inte behöver räkna igenom alla ordrar. Uppdateras av MergeOrders.
------------------------------------------------------------------------------
IF OBJECT_ID(N'sitoo.DailySales', N'U') IS NULL
CREATE TABLE sitoo.DailySales (
    Dag                  DATE          NOT NULL CONSTRAINT PK_DailySales PRIMARY KEY,
    Forsaljning          DECIMAL(19,2) NOT NULL,   -- exkl. moms
    ForsaljningInklMoms  DECIMAL(19,2) NOT NULL,
    Kvitton              INT           NOT NULL,
    Artiklar             DECIMAL(19,3) NULL,       -- sålda artiklar (antal)
    ReaArtiklar          DECIMAL(19,3) NULL,       -- varav sålda med minst @ReaRabatt rabatt
    AndelRea             DECIMAL(9,4)  NULL,       -- ReaArtiklar / Artiklar
    ArReadag             BIT           NULL,       -- 1 om AndelRea >= @ReaAndel
    UpdatedUtc           DATETIME2(0)  NOT NULL
);
GO
-- Äldre versioner saknade rea-kolumnerna
IF COL_LENGTH(N'sitoo.DailySales', N'ArReadag') IS NULL
    ALTER TABLE sitoo.DailySales ADD
        Artiklar    DECIMAL(19,3) NULL,
        ReaArtiklar DECIMAL(19,3) NULL,
        AndelRea    DECIMAL(9,4)  NULL,
        ArReadag    BIT           NULL;
GO
IF EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_DailySales_Forsaljning' AND object_id = OBJECT_ID(N'sitoo.DailySales'))
    DROP INDEX IX_DailySales_Forsaljning ON sitoo.DailySales;
GO

-- Räknar om DailySales för dagarna @FromDay–@ToDay. Utan datum: alla dagar.
--
-- Readag: minst @ReaAndel (90 %) av dagens sålda artiklar har sålts med minst
-- @ReaRabatt (30 %) rabatt. I Sitoo ligger en rabatt som en egen orderrad
-- (orderitemtype 20) med samma artikelnummer som artikelraden (orderitemtype 10).
-- Rabatter på hela köpet (rabattrader utan artikelnummer) fördelas på köpets
-- artiklar efter belopp. Rader med belopp 0 (t.ex. in-/utloggning) räknas inte.
CREATE OR ALTER PROCEDURE sitoo.RefreshDailySales
    @FromDay   DATE = NULL,
    @ToDay     DATE = NULL,
    @ReaRabatt DECIMAL(9,4) = 0.30,
    @ReaAndel  DECIMAL(9,4) = 0.90
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @now DATETIME2(0) = SYSUTCDATETIME();
    DECLARE @from DATETIME2(0) = ISNULL(CAST(@FromDay AS DATETIME2(0)), '19000101');
    DECLARE @to   DATETIME2(0) = CASE WHEN @ToDay IS NULL THEN '99991230'
                                      ELSE DATEADD(DAY, 1, CAST(@ToDay AS DATETIME2(0))) END;
    -- Liten marginal för avrundning (t.ex. 21,88 av 43,75 kr)
    DECLARE @limit DECIMAL(9,4) = @ReaRabatt - 0.005;

    SELECT OrderId, CAST(OrderDateLocal AS date) AS Dag,
           MoneyFinalNet, MoneyTotalGrossAll
    INTO #o
    FROM sitoo.Orders
    WHERE OrderDateLocal >= @from AND OrderDateLocal < @to;

    CREATE CLUSTERED INDEX IX_o ON #o (OrderId);

    -- Artikelrader och rabattrader per order och artikelnummer
    SELECT i.OrderId, o.Dag, NULLIF(i.Sku, N'') AS Sku,
           SUM(CASE WHEN i.OrderItemType = 10 THEN ISNULL(i.Quantity, 0) ELSE 0 END)           AS Qty,
           SUM(CASE WHEN i.OrderItemType = 10 THEN ISNULL(i.MoneyItemTotalNet, 0) ELSE 0 END)  AS P,
           SUM(CASE WHEN i.OrderItemType = 20 THEN -ISNULL(i.MoneyItemTotalNet, 0) ELSE 0 END) AS D
    INTO #l
    FROM sitoo.OrderItems i
    JOIN #o o ON o.OrderId = i.OrderId
    GROUP BY i.OrderId, o.Dag, NULLIF(i.Sku, N'');

    SELECT l.Dag,
           SUM(l.Qty) AS Artiklar,
           SUM(CASE WHEN (l.D + ISNULL(c.D * l.P / NULLIF(t.P, 0), 0)) / l.P >= @limit
                    THEN l.Qty ELSE 0 END) AS ReaArtiklar
    INTO #r
    FROM #l l
    -- Rabatter på hela köpet (utan artikelnummer)
    LEFT JOIN (SELECT OrderId, SUM(D) AS D FROM #l WHERE Sku IS NULL GROUP BY OrderId) c
           ON c.OrderId = l.OrderId
    -- Köpets totala artikelbelopp, för att fördela köprabatten
    LEFT JOIN (SELECT OrderId, SUM(P) AS P FROM #l WHERE Sku IS NOT NULL AND P > 0 AND Qty > 0 GROUP BY OrderId) t
           ON t.OrderId = l.OrderId
    WHERE l.Sku IS NOT NULL AND l.P > 0 AND l.Qty > 0
    GROUP BY l.Dag;

    MERGE sitoo.DailySales AS t
    USING (
        SELECT o.Dag,
               SUM(ISNULL(o.MoneyFinalNet, 0))                             AS Forsaljning,
               SUM(ISNULL(o.MoneyTotalGrossAll, 0))                        AS ForsaljningInklMoms,
               SUM(CASE WHEN o.MoneyTotalGrossAll > 0 THEN 1 ELSE 0 END)   AS Kvitton,
               MAX(r.Artiklar)                                             AS Artiklar,
               MAX(r.ReaArtiklar)                                          AS ReaArtiklar
        FROM #o o
        LEFT JOIN #r r ON r.Dag = o.Dag
        GROUP BY o.Dag
    ) AS s
        ON t.Dag = s.Dag
    WHEN MATCHED THEN UPDATE SET
        Forsaljning = s.Forsaljning, ForsaljningInklMoms = s.ForsaljningInklMoms, Kvitton = s.Kvitton,
        Artiklar = ISNULL(s.Artiklar, 0), ReaArtiklar = ISNULL(s.ReaArtiklar, 0),
        AndelRea = CASE WHEN s.Artiklar > 0 THEN s.ReaArtiklar / s.Artiklar ELSE 0 END,
        ArReadag = CASE WHEN s.Artiklar > 0 AND s.ReaArtiklar / s.Artiklar >= @ReaAndel THEN 1 ELSE 0 END,
        UpdatedUtc = @now
    WHEN NOT MATCHED THEN INSERT
        (Dag, Forsaljning, ForsaljningInklMoms, Kvitton, Artiklar, ReaArtiklar, AndelRea, ArReadag, UpdatedUtc)
        VALUES (s.Dag, s.Forsaljning, s.ForsaljningInklMoms, s.Kvitton,
                ISNULL(s.Artiklar, 0), ISNULL(s.ReaArtiklar, 0),
                CASE WHEN s.Artiklar > 0 THEN s.ReaArtiklar / s.Artiklar ELSE 0 END,
                CASE WHEN s.Artiklar > 0 AND s.ReaArtiklar / s.Artiklar >= @ReaAndel THEN 1 ELSE 0 END,
                @now)
    WHEN NOT MATCHED BY SOURCE AND t.Dag >= CAST(@from AS date) AND t.Dag < CAST(@to AS date) THEN DELETE;
END;
GO

------------------------------------------------------------------------------
-- MERGE-procedurer. Varje procedur tar emot en JSON-array direkt från Sitoo.
-- Belopp och decimaltal tolkas med TRY_CONVERT så att ett oväntat värde
-- blir NULL i stället för att stoppa hela synken.
------------------------------------------------------------------------------
CREATE OR ALTER PROCEDURE sitoo.MergeStores
    @SiteId INT,
    @Json   NVARCHAR(MAX),
    @FullSync BIT = 1
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;
    DECLARE @now DATETIME2(0) = SYSUTCDATETIME();

    SELECT r.[value] AS RawJson, j.*
    INTO #src
    FROM OPENJSON(@Json) r
    CROSS APPLY OPENJSON(r.[value]) WITH (
        StoreId         INT           '$.storeid',
        EshopId         INT           '$.eshopid',
        StoreType       INT           '$.storetype',
        Name            NVARCHAR(255) '$.name',
        ExternalId      NVARCHAR(255) '$.externalid',
        ExternalGroupId NVARCHAR(255) '$.externalgroupid',
        Phone           NVARCHAR(100) '$.phone',
        Email           NVARCHAR(320) '$.email',
        Address         NVARCHAR(255) '$.address',
        Address2        NVARCHAR(255) '$.address2',
        Zip             NVARCHAR(50)  '$.zip',
        City            NVARCHAR(255) '$.city',
        State           NVARCHAR(100) '$.state',
        CountryId       NVARCHAR(10)  '$.countryid',
        Latitude        NVARCHAR(30)  '$.latitude',
        Longitude       NVARCHAR(30)  '$.longitude'
    ) j
    WHERE j.StoreId IS NOT NULL;

    BEGIN TRAN;

    MERGE sitoo.Stores AS t
    USING #src AS s
        ON t.SiteId = @SiteId AND t.StoreId = s.StoreId
    WHEN MATCHED THEN UPDATE SET
        EshopId = s.EshopId, StoreType = s.StoreType, Name = s.Name, ExternalId = s.ExternalId,
        ExternalGroupId = s.ExternalGroupId, Phone = s.Phone, Email = s.Email, Address = s.Address,
        Address2 = s.Address2, Zip = s.Zip, City = s.City, State = s.State, CountryId = s.CountryId,
        Latitude = s.Latitude, Longitude = s.Longitude, IsDeleted = 0, RawJson = COMPRESS(s.RawJson), SyncedUtc = @now
    WHEN NOT MATCHED THEN INSERT
        (SiteId, StoreId, EshopId, StoreType, Name, ExternalId, ExternalGroupId, Phone, Email, Address,
         Address2, Zip, City, State, CountryId, Latitude, Longitude, IsDeleted, RawJson, SyncedUtc)
    VALUES
        (@SiteId, s.StoreId, s.EshopId, s.StoreType, s.Name, s.ExternalId, s.ExternalGroupId, s.Phone, s.Email, s.Address,
         s.Address2, s.Zip, s.City, s.State, s.CountryId, s.Latitude, s.Longitude, 0, COMPRESS(s.RawJson), @now);

    DECLARE @rows INT = @@ROWCOUNT;

    -- Butiker som inte längre finns i Sitoo markeras som borttagna (aldrig om listan är tom).
    IF @FullSync = 1 AND EXISTS (SELECT 1 FROM #src)
        UPDATE sitoo.Stores SET IsDeleted = 1, SyncedUtc = @now
        WHERE SiteId = @SiteId AND IsDeleted = 0
          AND StoreId NOT IN (SELECT StoreId FROM #src);

    COMMIT;
    SELECT @rows AS AffectedRows;
END;
GO

CREATE OR ALTER PROCEDURE sitoo.MergeWarehouses
    @SiteId INT,
    @Json   NVARCHAR(MAX),
    @FullSync BIT = 1
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;
    DECLARE @now DATETIME2(0) = SYSUTCDATETIME();

    SELECT r.[value] AS RawJson, j.*
    INTO #src
    FROM OPENJSON(@Json) r
    CROSS APPLY OPENJSON(r.[value]) WITH (
        WarehouseId   INT           '$.warehouseid',
        WarehouseType INT           '$.warehousetype',
        UseType       INT           '$.usetype',
        StoreId       INT           '$.storeid',
        Sellable      BIT           '$.sellable',
        CurrencyCode  NVARCHAR(3)   '$.currencycode',
        Name          NVARCHAR(255) '$.name',
        ExternalId    NVARCHAR(255) '$.externalid',
        Address       NVARCHAR(255) '$.address',
        Zip           NVARCHAR(50)  '$.zip',
        City          NVARCHAR(255) '$.city',
        CountryId     NVARCHAR(10)  '$.countryid'
    ) j
    WHERE j.WarehouseId IS NOT NULL;

    BEGIN TRAN;

    MERGE sitoo.Warehouses AS t
    USING #src AS s
        ON t.SiteId = @SiteId AND t.WarehouseId = s.WarehouseId
    WHEN MATCHED THEN UPDATE SET
        WarehouseType = s.WarehouseType, UseType = s.UseType, StoreId = s.StoreId, Sellable = s.Sellable,
        CurrencyCode = s.CurrencyCode, Name = s.Name, ExternalId = s.ExternalId, Address = s.Address,
        Zip = s.Zip, City = s.City, CountryId = s.CountryId, IsDeleted = 0, RawJson = COMPRESS(s.RawJson), SyncedUtc = @now
    WHEN NOT MATCHED THEN INSERT
        (SiteId, WarehouseId, WarehouseType, UseType, StoreId, Sellable, CurrencyCode, Name, ExternalId,
         Address, Zip, City, CountryId, IsDeleted, RawJson, SyncedUtc)
    VALUES
        (@SiteId, s.WarehouseId, s.WarehouseType, s.UseType, s.StoreId, s.Sellable, s.CurrencyCode, s.Name, s.ExternalId,
         s.Address, s.Zip, s.City, s.CountryId, 0, COMPRESS(s.RawJson), @now);

    DECLARE @rows INT = @@ROWCOUNT;

    IF @FullSync = 1 AND EXISTS (SELECT 1 FROM #src)
        UPDATE sitoo.Warehouses SET IsDeleted = 1, SyncedUtc = @now
        WHERE SiteId = @SiteId AND IsDeleted = 0
          AND WarehouseId NOT IN (SELECT WarehouseId FROM #src);

    COMMIT;
    SELECT @rows AS AffectedRows;
END;
GO

------------------------------------------------------------------------------
-- Leverantörer. Hela listan hämtas varje gång (FullSync).
------------------------------------------------------------------------------
CREATE OR ALTER PROCEDURE sitoo.MergeManufacturers
    @SiteId INT,
    @Json   NVARCHAR(MAX),
    @FullSync BIT = 1
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;
    DECLARE @now DATETIME2(0) = SYSUTCDATETIME();

    SELECT r.[value] AS RawJson,
           COALESCE(j.ExternalCompanyId, j.ManufacturerIdAlt) AS ManufacturerId, j.Name
    INTO #src
    FROM OPENJSON(@Json) r
    CROSS APPLY OPENJSON(r.[value]) WITH (
        -- Sitoo kallar leverantörens id externalcompanyid (= ManufacturerId på produkten)
        ExternalCompanyId INT        '$.externalcompanyid',
        ManufacturerIdAlt INT        '$.manufacturerid',
        Name           NVARCHAR(255) '$.name'
    ) j
    WHERE COALESCE(j.ExternalCompanyId, j.ManufacturerIdAlt) IS NOT NULL;

    BEGIN TRAN;

    MERGE sitoo.Manufacturers AS t
    USING #src AS s
        ON t.SiteId = @SiteId AND t.ManufacturerId = s.ManufacturerId
    WHEN MATCHED THEN UPDATE SET
        Name = s.Name, IsDeleted = 0, RawJson = COMPRESS(s.RawJson), SyncedUtc = @now
    WHEN NOT MATCHED THEN INSERT
        (SiteId, ManufacturerId, Name, IsDeleted, RawJson, SyncedUtc)
    VALUES
        (@SiteId, s.ManufacturerId, s.Name, 0, COMPRESS(s.RawJson), @now);

    DECLARE @rows INT = @@ROWCOUNT;

    IF @FullSync = 1 AND EXISTS (SELECT 1 FROM #src)
        UPDATE sitoo.Manufacturers SET IsDeleted = 1, SyncedUtc = @now
        WHERE SiteId = @SiteId AND IsDeleted = 0
          AND ManufacturerId NOT IN (SELECT ManufacturerId FROM #src);

    COMMIT;
    SELECT @rows AS AffectedRows;
END;
GO

CREATE OR ALTER PROCEDURE sitoo.MergeProducts
    @SiteId INT,
    @Json   NVARCHAR(MAX)
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @now DATETIME2(0) = SYSUTCDATETIME();

    MERGE sitoo.Products AS t
    USING (
        SELECT
            j.ProductId, j.Sku, j.Title, j.VariantParentId, j.Barcode, j.Active, j.ActivePos, j.VatId,
            TRY_CONVERT(DECIMAL(9,4),  j.VatValue)        AS VatValue,
            j.ManufacturerId, j.DefaultCategoryId,
            TRY_CONVERT(DECIMAL(19,2), j.MoneyPrice)      AS MoneyPrice,
            TRY_CONVERT(DECIMAL(19,2), j.MoneyPriceOrg)   AS MoneyPriceOrg,
            TRY_CONVERT(DECIMAL(19,2), j.MoneyPriceIn)    AS MoneyPriceIn,
            TRY_CONVERT(DECIMAL(19,2), j.MoneyFinalPrice) AS MoneyFinalPrice,
            j.StockCountEnable, j.UnitLabel, j.SkuManufacturer,
            sitoo.FromUnix(j.DateCreated)  AS DateCreatedUtc,
            sitoo.FromUnix(j.DateModified) AS DateModifiedUtc,
            r.[value] AS RawJson
        FROM OPENJSON(@Json) r
        CROSS APPLY OPENJSON(r.[value]) WITH (
            ProductId         INT           '$.productid',
            Sku               NVARCHAR(255) '$.sku',
            Title             NVARCHAR(500) '$.title',
            VariantParentId   INT           '$.variantparentid',
            Barcode           NVARCHAR(255) '$.barcode',
            Active            BIT           '$.active',
            ActivePos         BIT           '$.activepos',
            VatId             INT           '$.vatid',
            VatValue          NVARCHAR(40)  '$.vatvalue',
            ManufacturerId    INT           '$.manufacturerid',
            DefaultCategoryId INT           '$.defaultcategoryid',
            MoneyPrice        NVARCHAR(40)  '$.moneyprice',
            MoneyPriceOrg     NVARCHAR(40)  '$.moneypriceorg',
            MoneyPriceIn      NVARCHAR(40)  '$.moneypricein',
            MoneyFinalPrice   NVARCHAR(40)  '$.moneyfinalprice',
            StockCountEnable  BIT           '$.stockcountenable',
            UnitLabel         NVARCHAR(50)  '$.unitlabel',
            SkuManufacturer   NVARCHAR(255) '$.skumanufacturer',
            DateCreated       BIGINT        '$.datecreated',
            DateModified      BIGINT        '$.datemodified'
        ) j
        WHERE j.ProductId IS NOT NULL
    ) AS s
        ON t.SiteId = @SiteId AND t.ProductId = s.ProductId
    WHEN MATCHED THEN UPDATE SET
        Sku = s.Sku, Title = s.Title, VariantParentId = s.VariantParentId, Barcode = s.Barcode,
        Active = s.Active, ActivePos = s.ActivePos, VatId = s.VatId, VatValue = s.VatValue,
        ManufacturerId = s.ManufacturerId, DefaultCategoryId = s.DefaultCategoryId,
        MoneyPrice = s.MoneyPrice, MoneyPriceOrg = s.MoneyPriceOrg, MoneyPriceIn = s.MoneyPriceIn,
        MoneyFinalPrice = s.MoneyFinalPrice, StockCountEnable = s.StockCountEnable, UnitLabel = s.UnitLabel,
        SkuManufacturer = s.SkuManufacturer, DateCreatedUtc = s.DateCreatedUtc, DateModifiedUtc = s.DateModifiedUtc,
        IsDeleted = 0, DeletedUtc = NULL, RawJson = COMPRESS(s.RawJson), SyncedUtc = @now
    WHEN NOT MATCHED THEN INSERT
        (SiteId, ProductId, Sku, Title, VariantParentId, Barcode, Active, ActivePos, VatId, VatValue,
         ManufacturerId, DefaultCategoryId, MoneyPrice, MoneyPriceOrg, MoneyPriceIn, MoneyFinalPrice,
         StockCountEnable, UnitLabel, SkuManufacturer, DateCreatedUtc, DateModifiedUtc, IsDeleted, RawJson, SyncedUtc)
    VALUES
        (@SiteId, s.ProductId, s.Sku, s.Title, s.VariantParentId, s.Barcode, s.Active, s.ActivePos, s.VatId, s.VatValue,
         s.ManufacturerId, s.DefaultCategoryId, s.MoneyPrice, s.MoneyPriceOrg, s.MoneyPriceIn, s.MoneyFinalPrice,
         s.StockCountEnable, s.UnitLabel, s.SkuManufacturer, s.DateCreatedUtc, s.DateModifiedUtc, 0, COMPRESS(s.RawJson), @now);

    SELECT @@ROWCOUNT AS AffectedRows;
END;
GO

-- Tar emot en JSON-array med ALLA productid som finns i Sitoo och markerar resten som borttagna.
CREATE OR ALTER PROCEDURE sitoo.MarkDeletedProducts
    @SiteId  INT,
    @IdsJson NVARCHAR(MAX)
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @now DATETIME2(0) = SYSUTCDATETIME();
    DECLARE @ids TABLE (ProductId INT NOT NULL PRIMARY KEY);

    INSERT @ids (ProductId)
    SELECT DISTINCT TRY_CONVERT(INT, [value]) FROM OPENJSON(@IdsJson)
    WHERE TRY_CONVERT(INT, [value]) IS NOT NULL;

    IF NOT EXISTS (SELECT 1 FROM @ids)
    BEGIN
        SELECT 0 AS AffectedRows;
        RETURN;
    END;

    UPDATE p SET IsDeleted = 1, DeletedUtc = @now, SyncedUtc = @now
    FROM sitoo.Products p
    WHERE p.SiteId = @SiteId AND p.IsDeleted = 0
      AND NOT EXISTS (SELECT 1 FROM @ids i WHERE i.ProductId = p.ProductId);

    SELECT @@ROWCOUNT AS AffectedRows;
END;
GO

CREATE OR ALTER PROCEDURE sitoo.MergeStockItems
    @SiteId      INT,
    @WarehouseId INT,
    @Json        NVARCHAR(MAX)
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @now DATETIME2(0) = SYSUTCDATETIME();

    MERGE sitoo.StockItems AS t
    USING (
        SELECT
            j.Sku, j.WarehouseItemId, j.BinLocation,
            TRY_CONVERT(DECIMAL(19,3), j.DecimalThreshold) AS DecimalThreshold,
            TRY_CONVERT(DECIMAL(19,3), j.DecimalTotal)     AS DecimalTotal,
            TRY_CONVERT(DECIMAL(19,3), j.DecimalReserved)  AS DecimalReserved,
            TRY_CONVERT(DECIMAL(19,3), j.DecimalAvailable) AS DecimalAvailable,
            TRY_CONVERT(DECIMAL(19,2), j.MoneyTotal)       AS MoneyTotal,
            sitoo.FromUnix(j.DateLastModified)    AS DateLastModifiedUtc,
            sitoo.FromUnix(j.DateLastStocktaking) AS DateLastStocktakingUtc,
            r.[value] AS RawJson
        FROM OPENJSON(@Json) r
        CROSS APPLY OPENJSON(r.[value]) WITH (
            WarehouseItemId     BIGINT        '$.warehouseitemid',
            Sku                 NVARCHAR(255) '$.sku',
            BinLocation         NVARCHAR(100) '$.binlocation',
            DecimalThreshold    NVARCHAR(40)  '$.decimalthreshold',
            DecimalTotal        NVARCHAR(40)  '$.decimaltotal',
            DecimalReserved     NVARCHAR(40)  '$.decimalreserved',
            DecimalAvailable    NVARCHAR(40)  '$.decimalavailable',
            MoneyTotal          NVARCHAR(40)  '$.moneytotal',
            DateLastModified    BIGINT        '$.datelastmodified',
            DateLastStocktaking BIGINT        '$.datelaststocktaking'
        ) j
        WHERE j.Sku IS NOT NULL
    ) AS s
        ON t.SiteId = @SiteId AND t.WarehouseId = @WarehouseId AND t.Sku = s.Sku
    WHEN MATCHED THEN UPDATE SET
        WarehouseItemId = s.WarehouseItemId, BinLocation = s.BinLocation, DecimalThreshold = s.DecimalThreshold,
        DecimalTotal = s.DecimalTotal, DecimalReserved = s.DecimalReserved, DecimalAvailable = s.DecimalAvailable,
        MoneyTotal = s.MoneyTotal, DateLastModifiedUtc = s.DateLastModifiedUtc,
        DateLastStocktakingUtc = s.DateLastStocktakingUtc, RawJson = COMPRESS(s.RawJson), SyncedUtc = @now
    WHEN NOT MATCHED THEN INSERT
        (SiteId, WarehouseId, Sku, WarehouseItemId, BinLocation, DecimalThreshold, DecimalTotal, DecimalReserved,
         DecimalAvailable, MoneyTotal, DateLastModifiedUtc, DateLastStocktakingUtc, RawJson, SyncedUtc)
    VALUES
        (@SiteId, @WarehouseId, s.Sku, s.WarehouseItemId, s.BinLocation, s.DecimalThreshold, s.DecimalTotal, s.DecimalReserved,
         s.DecimalAvailable, s.MoneyTotal, s.DateLastModifiedUtc, s.DateLastStocktakingUtc, COMPRESS(s.RawJson), @now);

    SELECT @@ROWCOUNT AS AffectedRows;
END;
GO

CREATE OR ALTER PROCEDURE sitoo.MergeOrders
    @Json NVARCHAR(MAX)
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;
    DECLARE @now DATETIME2(0) = SYSUTCDATETIME();

    SELECT
        r.[value] AS RawJson,
        j.OrderId, j.EshopId, j.ExternalId, j.CreditOrderId, j.CartId,
        sitoo.FromUnix(j.OrderDate) AS OrderDateUtc,
        CAST(sitoo.FromUnix(j.OrderDate) AT TIME ZONE 'UTC' AT TIME ZONE 'W. Europe Standard Time' AS DATETIME2(0)) AS OrderDateLocal,
        j.OrderStateId, j.PaymentStateId, j.OrderTypeId, j.RegisterId, j.WarehouseId, j.UserIdStaff,
        j.CurrencyCode, j.Email, j.NameFirst, j.NameLast, j.PersonalId, j.Company, j.Phone, j.CustomerRef,
        TRY_CONVERT(DECIMAL(19,2), j.MoneyTotalNet)           AS MoneyTotalNet,
        TRY_CONVERT(DECIMAL(19,2), j.MoneyTotalVat)           AS MoneyTotalVat,
        TRY_CONVERT(DECIMAL(19,2), j.MoneyFinalNet)           AS MoneyFinalNet,
        TRY_CONVERT(DECIMAL(19,2), j.MoneyFinalVat)           AS MoneyFinalVat,
        TRY_CONVERT(DECIMAL(19,2), j.MoneyFinalSalesTax)      AS MoneyFinalSalesTax,
        TRY_CONVERT(DECIMAL(19,2), j.MoneyTotalGrossRoundoff) AS MoneyTotalGrossRoundoff,
        TRY_CONVERT(DECIMAL(19,2), j.MoneyTotalGrossAll)      AS MoneyTotalGrossAll,
        j.Comment
    INTO #src
    FROM OPENJSON(@Json) r
    CROSS APPLY OPENJSON(r.[value]) WITH (
        OrderId                 BIGINT        '$.orderid',
        EshopId                 INT           '$.eshopid',
        ExternalId              NVARCHAR(255) '$.externalid',
        CreditOrderId           BIGINT        '$.creditorderid',
        CartId                  NVARCHAR(64)  '$.cartid',
        OrderDate               BIGINT        '$.orderdate',
        OrderStateId            INT           '$.orderstateid',
        PaymentStateId          INT           '$.paymentstateid',
        OrderTypeId             INT           '$.ordertypeid',
        RegisterId              NVARCHAR(100) '$.registerid',
        WarehouseId             INT           '$.warehouseid',
        UserIdStaff             NVARCHAR(64)  '$.userid_staff',
        CurrencyCode            NVARCHAR(3)   '$.currencycode',
        Email                   NVARCHAR(320) '$.email',
        NameFirst               NVARCHAR(255) '$.namefirst',
        NameLast                NVARCHAR(255) '$.namelast',
        PersonalId              NVARCHAR(50)  '$.personalid',
        Company                 NVARCHAR(255) '$.company',
        Phone                   NVARCHAR(100) '$.phone',
        CustomerRef             NVARCHAR(255) '$.customerref',
        MoneyTotalNet           NVARCHAR(40)  '$.moneytotal_net',
        MoneyTotalVat           NVARCHAR(40)  '$.moneytotal_vat',
        MoneyFinalNet           NVARCHAR(40)  '$.moneyfinal_net',
        MoneyFinalVat           NVARCHAR(40)  '$.moneyfinal_vat',
        MoneyFinalSalesTax      NVARCHAR(40)  '$.moneyfinal_salestax',
        MoneyTotalGrossRoundoff NVARCHAR(40)  '$.moneytotal_gross_roundoff',
        MoneyTotalGrossAll      NVARCHAR(40)  '$.moneytotal_gross_all',
        Comment                 NVARCHAR(MAX) '$.comment'
    ) j
    WHERE j.OrderId IS NOT NULL;

    BEGIN TRAN;

    MERGE sitoo.Orders AS t
    USING #src AS s
        ON t.OrderId = s.OrderId
    WHEN MATCHED THEN UPDATE SET
        EshopId = s.EshopId, ExternalId = s.ExternalId, CreditOrderId = s.CreditOrderId, CartId = s.CartId,
        OrderDateUtc = s.OrderDateUtc, OrderDateLocal = s.OrderDateLocal, OrderStateId = s.OrderStateId, PaymentStateId = s.PaymentStateId,
        OrderTypeId = s.OrderTypeId, RegisterId = s.RegisterId, WarehouseId = s.WarehouseId,
        UserIdStaff = s.UserIdStaff, CurrencyCode = s.CurrencyCode, Email = s.Email, NameFirst = s.NameFirst,
        NameLast = s.NameLast, PersonalId = s.PersonalId, Company = s.Company, Phone = s.Phone,
        CustomerRef = s.CustomerRef, MoneyTotalNet = s.MoneyTotalNet, MoneyTotalVat = s.MoneyTotalVat,
        MoneyFinalNet = s.MoneyFinalNet, MoneyFinalVat = s.MoneyFinalVat, MoneyFinalSalesTax = s.MoneyFinalSalesTax,
        MoneyTotalGrossRoundoff = s.MoneyTotalGrossRoundoff, MoneyTotalGrossAll = s.MoneyTotalGrossAll,
        Comment = s.Comment, RawJson = COMPRESS(s.RawJson), SyncedUtc = @now
    WHEN NOT MATCHED THEN INSERT
        (OrderId, EshopId, ExternalId, CreditOrderId, CartId, OrderDateUtc, OrderDateLocal, OrderStateId, PaymentStateId,
         OrderTypeId, RegisterId, WarehouseId, UserIdStaff, CurrencyCode, Email, NameFirst, NameLast,
         PersonalId, Company, Phone, CustomerRef, MoneyTotalNet, MoneyTotalVat, MoneyFinalNet, MoneyFinalVat,
         MoneyFinalSalesTax, MoneyTotalGrossRoundoff, MoneyTotalGrossAll, Comment, RawJson, SyncedUtc)
    VALUES
        (s.OrderId, s.EshopId, s.ExternalId, s.CreditOrderId, s.CartId, s.OrderDateUtc, s.OrderDateLocal, s.OrderStateId, s.PaymentStateId,
         s.OrderTypeId, s.RegisterId, s.WarehouseId, s.UserIdStaff, s.CurrencyCode, s.Email, s.NameFirst, s.NameLast,
         s.PersonalId, s.Company, s.Phone, s.CustomerRef, s.MoneyTotalNet, s.MoneyTotalVat, s.MoneyFinalNet, s.MoneyFinalVat,
         s.MoneyFinalSalesTax, s.MoneyTotalGrossRoundoff, s.MoneyTotalGrossAll, s.Comment, COMPRESS(s.RawJson), @now);

    DECLARE @rows INT = @@ROWCOUNT;

    -- Orderrader och betalningar ersätts helt för de ordrar som kom i den här omgången.
    DELETE FROM sitoo.OrderItems    WHERE OrderId IN (SELECT OrderId FROM #src);
    DELETE FROM sitoo.OrderPayments WHERE OrderId IN (SELECT OrderId FROM #src);

    INSERT sitoo.OrderItems
        (OrderId, LineNumber, OrderItemId, OrderItemType, ProductId, Sku, ProductName, Quantity,
         DecimalQuantityTotal, UnitLabel, VatValue, MoneyNetPricePerUnit, MoneyPriceOrg, MoneyOriginalPrice,
         MoneyDiscountedPrice, MoneyDiscount, MoneyItemTotalNet, MoneyItemTotalVat, VoucherId, VoucherCode,
         VoucherName)
    SELECT
        s.OrderId,
        CAST(oi.[key] AS INT) + 1,
        it.OrderItemId, it.OrderItemType, it.ProductId, it.Sku, it.ProductName,
        TRY_CONVERT(DECIMAL(19,3), it.Quantity),
        TRY_CONVERT(DECIMAL(19,3), it.DecimalQuantityTotal),
        it.UnitLabel,
        TRY_CONVERT(DECIMAL(9,4),  it.VatValue),
        TRY_CONVERT(DECIMAL(19,2), it.MoneyNetPricePerUnit),
        TRY_CONVERT(DECIMAL(19,2), it.MoneyPriceOrg),
        TRY_CONVERT(DECIMAL(19,2), it.MoneyOriginalPrice),
        TRY_CONVERT(DECIMAL(19,2), it.MoneyDiscountedPrice),
        TRY_CONVERT(DECIMAL(19,2), it.MoneyDiscount),
        TRY_CONVERT(DECIMAL(19,2), it.MoneyItemTotalNet),
        TRY_CONVERT(DECIMAL(19,2), it.MoneyItemTotalVat),
        it.VoucherId, it.VoucherCode, it.VoucherName
    FROM #src s
    CROSS APPLY OPENJSON(s.RawJson, '$.orderitems') oi
    CROSS APPLY OPENJSON(oi.[value]) WITH (
        OrderItemId          BIGINT        '$.orderitemid',
        OrderItemType        INT           '$.orderitemtype',
        ProductId            INT           '$.productid',
        Sku                  NVARCHAR(255) '$.sku',
        ProductName          NVARCHAR(500) '$.productname',
        Quantity             NVARCHAR(40)  '$.quantity',
        DecimalQuantityTotal NVARCHAR(40)  '$.decimalquantitytotal',
        UnitLabel            NVARCHAR(50)  '$.unitlabel',
        VatValue             NVARCHAR(40)  '$.vatvalue',
        MoneyNetPricePerUnit NVARCHAR(40)  '$.moneynetpriceperunit',
        MoneyPriceOrg        NVARCHAR(40)  '$.moneypriceorg',
        MoneyOriginalPrice   NVARCHAR(40)  '$.moneyoriginalprice',
        MoneyDiscountedPrice NVARCHAR(40)  '$.moneydiscountedprice',
        MoneyDiscount        NVARCHAR(40)  '$.moneydiscount',
        MoneyItemTotalNet    NVARCHAR(40)  '$.moneyitemtotal_net',
        MoneyItemTotalVat    NVARCHAR(40)  '$.moneyitemtotal_vat',
        VoucherId            INT           '$.voucherid',
        VoucherCode          NVARCHAR(255) '$.vouchercode',
        VoucherName          NVARCHAR(255) '$.vouchername'
    ) it;

    INSERT sitoo.OrderPayments (OrderId, LineNumber, Name, MoneyAmount)
    SELECT
        s.OrderId,
        CAST(p.[key] AS INT) + 1,
        pj.Name,
        TRY_CONVERT(DECIMAL(19,2), pj.MoneyAmount)
    FROM #src s
    CROSS APPLY OPENJSON(s.RawJson, '$.payments') p
    CROSS APPLY OPENJSON(p.[value]) WITH (
        Name        NVARCHAR(255) '$.name',
        MoneyAmount NVARCHAR(40)  '$.moneyamount'
    ) pj;

    COMMIT;

    -- Försäljning per dag för de dagar som berördes
    DECLARE @d1 DATE = (SELECT MIN(CAST(OrderDateLocal AS date)) FROM #src);
    DECLARE @d2 DATE = (SELECT MAX(CAST(OrderDateLocal AS date)) FROM #src);
    IF @d1 IS NOT NULL
        EXEC sitoo.RefreshDailySales @FromDay = @d1, @ToDay = @d2;
    SELECT @rows AS AffectedRows;
END;
GO

CREATE OR ALTER PROCEDURE sitoo.MergeZReports
    @Json NVARCHAR(MAX)
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;
    DECLARE @now DATETIME2(0) = SYSUTCDATETIME();

    SELECT
        r.[value] AS RawJson,
        j.RegisterId, j.ZReportId, j.EshopId, j.StoreExternalId, j.StoreName, j.RegisterNumber, j.Company,
        j.Staff, j.StaffUserId, j.CurrencyCode,
        sitoo.FromUnix(j.DateOpened)  AS DateOpenedUtc,
        sitoo.FromUnix(j.DateCreated) AS DateCreatedUtc,
        sitoo.FromUnix(j.DateAdded)   AS DateAddedUtc,
        TRY_CONVERT(DECIMAL(19,2), j.MoneySalesTotal)       AS MoneySalesTotal,
        TRY_CONVERT(DECIMAL(19,2), j.MoneySalesTotalNet)    AS MoneySalesTotalNet,
        j.NumSales, j.NumSalesItems,
        TRY_CONVERT(DECIMAL(19,2), j.MoneyRefundTotal)      AS MoneyRefundTotal,
        TRY_CONVERT(DECIMAL(19,2), j.MoneyRefundTotalNet)   AS MoneyRefundTotalNet,
        j.NumRefund, j.NumRefundItems,
        TRY_CONVERT(DECIMAL(19,2), j.MoneyDiscount)         AS MoneyDiscount,
        TRY_CONVERT(DECIMAL(19,2), j.MoneyRoundOff)         AS MoneyRoundOff,
        TRY_CONVERT(DECIMAL(19,2), j.MoneySummaryTotal)     AS MoneySummaryTotal,
        TRY_CONVERT(DECIMAL(19,2), j.MoneyCashExpected)     AS MoneyCashExpected,
        TRY_CONVERT(DECIMAL(19,2), j.MoneyCashCounted)      AS MoneyCashCounted,
        TRY_CONVERT(DECIMAL(19,2), j.MoneyCashDiff)         AS MoneyCashDiff,
        j.NumReceipts,
        TRY_CONVERT(DECIMAL(19,2), j.MoneyGrandTotalSales)  AS MoneyGrandTotalSales,
        TRY_CONVERT(DECIMAL(19,2), j.MoneyGrandTotalRefund) AS MoneyGrandTotalRefund,
        TRY_CONVERT(DECIMAL(19,2), j.MoneyGrandTotalNet)    AS MoneyGrandTotalNet
    INTO #src
    FROM OPENJSON(@Json) r
    CROSS APPLY OPENJSON(r.[value]) WITH (
        RegisterId            NVARCHAR(100) '$.registerid',
        ZReportId             INT           '$.zreportid',
        EshopId               INT           '$.eshopid',
        StoreExternalId       NVARCHAR(255) '$.store_id',
        StoreName             NVARCHAR(255) '$.store_name',
        RegisterNumber        INT           '$.registernumber',
        Company               NVARCHAR(255) '$.company',
        Staff                 NVARCHAR(255) '$.staff',
        StaffUserId           NVARCHAR(64)  '$.staffuserid',
        CurrencyCode          NVARCHAR(3)   '$.currencycode',
        DateOpened            BIGINT        '$.dateopened',
        DateCreated           BIGINT        '$.datecreated',
        DateAdded             BIGINT        '$.dateadded',
        MoneySalesTotal       NVARCHAR(40)  '$.moneysalestotal',
        MoneySalesTotalNet    NVARCHAR(40)  '$.moneysalestotalnet',
        NumSales              INT           '$.numsales',
        NumSalesItems         INT           '$.numsalesitems',
        MoneyRefundTotal      NVARCHAR(40)  '$.moneyrefundtotal',
        MoneyRefundTotalNet   NVARCHAR(40)  '$.moneyrefundtotalnet',
        NumRefund             INT           '$.numrefund',
        NumRefundItems        INT           '$.numrefunditems',
        MoneyDiscount         NVARCHAR(40)  '$.moneydiscount',
        MoneyRoundOff         NVARCHAR(40)  '$.moneyroundoff',
        MoneySummaryTotal     NVARCHAR(40)  '$.moneysummarytotal',
        MoneyCashExpected     NVARCHAR(40)  '$.moneycash_expected',
        MoneyCashCounted      NVARCHAR(40)  '$.moneycash_counted',
        MoneyCashDiff         NVARCHAR(40)  '$.moneycash_diff',
        NumReceipts           INT           '$.numreceipts',
        MoneyGrandTotalSales  NVARCHAR(40)  '$.moneygrandtotalsales',
        MoneyGrandTotalRefund NVARCHAR(40)  '$.moneygrandtotalrefund',
        MoneyGrandTotalNet    NVARCHAR(40)  '$.moneygrandtotalnet'
    ) j
    WHERE j.RegisterId IS NOT NULL AND j.ZReportId IS NOT NULL;

    BEGIN TRAN;

    MERGE sitoo.ZReports AS t
    USING #src AS s
        ON t.RegisterId = s.RegisterId AND t.ZReportId = s.ZReportId
    WHEN MATCHED THEN UPDATE SET
        EshopId = s.EshopId, StoreExternalId = s.StoreExternalId, StoreName = s.StoreName,
        RegisterNumber = s.RegisterNumber, Company = s.Company, Staff = s.Staff, StaffUserId = s.StaffUserId,
        CurrencyCode = s.CurrencyCode, DateOpenedUtc = s.DateOpenedUtc, DateCreatedUtc = s.DateCreatedUtc,
        DateAddedUtc = s.DateAddedUtc, MoneySalesTotal = s.MoneySalesTotal, MoneySalesTotalNet = s.MoneySalesTotalNet,
        NumSales = s.NumSales, NumSalesItems = s.NumSalesItems, MoneyRefundTotal = s.MoneyRefundTotal,
        MoneyRefundTotalNet = s.MoneyRefundTotalNet, NumRefund = s.NumRefund, NumRefundItems = s.NumRefundItems,
        MoneyDiscount = s.MoneyDiscount, MoneyRoundOff = s.MoneyRoundOff, MoneySummaryTotal = s.MoneySummaryTotal,
        MoneyCashExpected = s.MoneyCashExpected, MoneyCashCounted = s.MoneyCashCounted, MoneyCashDiff = s.MoneyCashDiff,
        NumReceipts = s.NumReceipts, MoneyGrandTotalSales = s.MoneyGrandTotalSales,
        MoneyGrandTotalRefund = s.MoneyGrandTotalRefund, MoneyGrandTotalNet = s.MoneyGrandTotalNet,
        RawJson = COMPRESS(s.RawJson), SyncedUtc = @now
    WHEN NOT MATCHED THEN INSERT
        (RegisterId, ZReportId, EshopId, StoreExternalId, StoreName, RegisterNumber, Company, Staff, StaffUserId,
         CurrencyCode, DateOpenedUtc, DateCreatedUtc, DateAddedUtc, MoneySalesTotal, MoneySalesTotalNet, NumSales,
         NumSalesItems, MoneyRefundTotal, MoneyRefundTotalNet, NumRefund, NumRefundItems, MoneyDiscount, MoneyRoundOff,
         MoneySummaryTotal, MoneyCashExpected, MoneyCashCounted, MoneyCashDiff, NumReceipts, MoneyGrandTotalSales,
         MoneyGrandTotalRefund, MoneyGrandTotalNet, RawJson, SyncedUtc)
    VALUES
        (s.RegisterId, s.ZReportId, s.EshopId, s.StoreExternalId, s.StoreName, s.RegisterNumber, s.Company, s.Staff, s.StaffUserId,
         s.CurrencyCode, s.DateOpenedUtc, s.DateCreatedUtc, s.DateAddedUtc, s.MoneySalesTotal, s.MoneySalesTotalNet, s.NumSales,
         s.NumSalesItems, s.MoneyRefundTotal, s.MoneyRefundTotalNet, s.NumRefund, s.NumRefundItems, s.MoneyDiscount, s.MoneyRoundOff,
         s.MoneySummaryTotal, s.MoneyCashExpected, s.MoneyCashCounted, s.MoneyCashDiff, s.NumReceipts, s.MoneyGrandTotalSales,
         s.MoneyGrandTotalRefund, s.MoneyGrandTotalNet, COMPRESS(s.RawJson), @now);

    DECLARE @rows INT = @@ROWCOUNT;

    DELETE c FROM sitoo.ZReportPayments c
        JOIN #src s ON s.RegisterId = c.RegisterId AND s.ZReportId = c.ZReportId;
    DELETE c FROM sitoo.ZReportVatGroups c
        JOIN #src s ON s.RegisterId = c.RegisterId AND s.ZReportId = c.ZReportId;
    DELETE c FROM sitoo.ZReportProductGroups c
        JOIN #src s ON s.RegisterId = c.RegisterId AND s.ZReportId = c.ZReportId;

    -- Betalningar
    INSERT sitoo.ZReportPayments (RegisterId, ZReportId, Kind, LineNumber, Name, MoneyTotal)
    SELECT s.RegisterId, s.ZReportId, x.Kind, CAST(x.[key] AS INT) + 1, pj.Name,
           TRY_CONVERT(DECIMAL(19,2), pj.MoneyTotal)
    FROM #src s
    CROSS APPLY (
        SELECT N'sales'  AS Kind, a.[key], a.[value] FROM OPENJSON(s.RawJson, '$.paymentssales')  a
        UNION ALL
        SELECT N'refund' AS Kind, a.[key], a.[value] FROM OPENJSON(s.RawJson, '$.paymentsrefund') a
    ) x
    CROSS APPLY OPENJSON(x.[value]) WITH (
        Name       NVARCHAR(255) '$.name',
        MoneyTotal NVARCHAR(40)  '$.moneytotal'
    ) pj;

    -- Momsgrupper
    INSERT sitoo.ZReportVatGroups (RegisterId, ZReportId, Kind, LineNumber, VatValue, MoneyTotal, MoneyTotalNet, MoneyTotalVat)
    SELECT s.RegisterId, s.ZReportId, x.Kind, CAST(x.[key] AS INT) + 1,
           TRY_CONVERT(DECIMAL(9,4),  vj.VatValue),
           TRY_CONVERT(DECIMAL(19,2), vj.MoneyTotal),
           TRY_CONVERT(DECIMAL(19,2), vj.MoneyTotalNet),
           TRY_CONVERT(DECIMAL(19,2), vj.MoneyTotalVat)
    FROM #src s
    CROSS APPLY (
        SELECT N'sales'             AS Kind, a.[key], a.[value] FROM OPENJSON(s.RawJson, '$.vatgroupssales') a
        UNION ALL
        SELECT N'salesnotdelivered' AS Kind, a.[key], a.[value] FROM OPENJSON(s.RawJson, '$.vatgroupssalesnotdelivered') a
        UNION ALL
        SELECT N'refund'            AS Kind, a.[key], a.[value] FROM OPENJSON(s.RawJson, '$.vatgroupsrefund') a
    ) x
    CROSS APPLY OPENJSON(x.[value]) WITH (
        VatValue      NVARCHAR(40) '$.vatvalue',
        MoneyTotal    NVARCHAR(40) '$.moneytotal',
        MoneyTotalNet NVARCHAR(40) '$.moneytotalnet',
        MoneyTotalVat NVARCHAR(40) '$.moneytotalvat'
    ) vj;

    -- Varugrupper
    INSERT sitoo.ZReportProductGroups
        (RegisterId, ZReportId, Kind, LineNumber, Name, VatValue, NumTotal, MoneyTotal, MoneyTotalNet, MoneyTotalVat)
    SELECT s.RegisterId, s.ZReportId, x.Kind, CAST(x.[key] AS INT) + 1, gj.Name,
           TRY_CONVERT(DECIMAL(9,4),  gj.VatValue),
           TRY_CONVERT(DECIMAL(19,3), gj.NumTotal),
           TRY_CONVERT(DECIMAL(19,2), gj.MoneyTotal),
           TRY_CONVERT(DECIMAL(19,2), gj.MoneyTotalNet),
           TRY_CONVERT(DECIMAL(19,2), gj.MoneyTotalVat)
    FROM #src s
    CROSS APPLY (
        SELECT N'sales'             AS Kind, a.[key], a.[value] FROM OPENJSON(s.RawJson, '$.productgroupssales') a
        UNION ALL
        SELECT N'salesnotdelivered' AS Kind, a.[key], a.[value] FROM OPENJSON(s.RawJson, '$.productgroupssalesnotdelivered') a
        UNION ALL
        SELECT N'refund'            AS Kind, a.[key], a.[value] FROM OPENJSON(s.RawJson, '$.productgroupsrefund') a
    ) x
    CROSS APPLY OPENJSON(x.[value]) WITH (
        Name          NVARCHAR(255) '$.name',
        VatValue      NVARCHAR(40)  '$.vatvalue',
        NumTotal      NVARCHAR(40)  '$.numtotal',
        MoneyTotal    NVARCHAR(40)  '$.moneytotal',
        MoneyTotalNet NVARCHAR(40)  '$.moneytotalnet',
        MoneyTotalVat NVARCHAR(40)  '$.moneytotalvat'
    ) gj;

    COMMIT;
    SELECT @rows AS AffectedRows;
END;
GO

CREATE OR ALTER PROCEDURE sitoo.MergeUsers
    @Json NVARCHAR(MAX)
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @now DATETIME2(0) = SYSUTCDATETIME();

    MERGE sitoo.Users AS t
    USING (
        SELECT
            j.UserId, j.Email, j.NameFirst, j.NameLast, j.PersonalId, j.ExternalId, j.CustomerNumber,
            j.Company, j.Department, j.CompanyId, j.Address, j.Address2, j.Zip, j.City, j.State,
            j.CountryId, j.Phone, j.Mobile, j.Role, j.Active, j.Notes,
            sitoo.FromUnix(j.DateCreated)  AS DateCreatedUtc,
            sitoo.FromUnix(j.DateModified) AS DateModifiedUtc,
            r.[value] AS RawJson
        FROM OPENJSON(@Json) r
        CROSS APPLY OPENJSON(r.[value]) WITH (
            UserId         NVARCHAR(64)  '$.userid',
            Email          NVARCHAR(320) '$.email',
            NameFirst      NVARCHAR(255) '$.namefirst',
            NameLast       NVARCHAR(255) '$.namelast',
            PersonalId     NVARCHAR(50)  '$.personalid',
            ExternalId     NVARCHAR(255) '$.externalid',
            CustomerNumber NVARCHAR(100) '$.customernumber',
            Company        NVARCHAR(255) '$.company',
            Department     NVARCHAR(255) '$.department',
            CompanyId      NVARCHAR(100) '$.companyid',
            Address        NVARCHAR(255) '$.address',
            Address2       NVARCHAR(255) '$.address2',
            Zip            NVARCHAR(50)  '$.zip',
            City           NVARCHAR(255) '$.city',
            State          NVARCHAR(100) '$.state',
            CountryId      NVARCHAR(10)  '$.countryid',
            Phone          NVARCHAR(100) '$.phone',
            Mobile         NVARCHAR(100) '$.mobile',
            Role           INT           '$.role',
            Active         BIT           '$.active',
            Notes          NVARCHAR(MAX) '$.notes',
            DateCreated    BIGINT        '$.datecreated',
            DateModified   BIGINT        '$.datemodified'
        ) j
        WHERE j.UserId IS NOT NULL
    ) AS s
        ON t.UserId = s.UserId
    WHEN MATCHED THEN UPDATE SET
        Email = s.Email, NameFirst = s.NameFirst, NameLast = s.NameLast, PersonalId = s.PersonalId,
        ExternalId = s.ExternalId, CustomerNumber = s.CustomerNumber, Company = s.Company,
        Department = s.Department, CompanyId = s.CompanyId, Address = s.Address, Address2 = s.Address2,
        Zip = s.Zip, City = s.City, State = s.State, CountryId = s.CountryId, Phone = s.Phone,
        Mobile = s.Mobile, Role = s.Role, Active = s.Active, Notes = s.Notes,
        DateCreatedUtc = s.DateCreatedUtc, DateModifiedUtc = s.DateModifiedUtc, RawJson = COMPRESS(s.RawJson), SyncedUtc = @now
    WHEN NOT MATCHED THEN INSERT
        (UserId, Email, NameFirst, NameLast, PersonalId, ExternalId, CustomerNumber, Company, Department,
         CompanyId, Address, Address2, Zip, City, State, CountryId, Phone, Mobile, Role, Active, Notes,
         DateCreatedUtc, DateModifiedUtc, RawJson, SyncedUtc)
    VALUES
        (s.UserId, s.Email, s.NameFirst, s.NameLast, s.PersonalId, s.ExternalId, s.CustomerNumber, s.Company, s.Department,
         s.CompanyId, s.Address, s.Address2, s.Zip, s.City, s.State, s.CountryId, s.Phone, s.Mobile, s.Role, s.Active, s.Notes,
         s.DateCreatedUtc, s.DateModifiedUtc, COMPRESS(s.RawJson), @now);

    SELECT @@ROWCOUNT AS AffectedRows;
END;
GO

------------------------------------------------------------------------------
-- Färdiga vyer för rapporter (svensk tid)
------------------------------------------------------------------------------
CREATE OR ALTER VIEW sitoo.vLagersaldo
AS
SELECT
    s.SiteId,
    w.WarehouseId,
    w.Name                  AS Lager,
    st.Name                 AS Butik,
    s.Sku,
    p.ProductId,
    p.Title                 AS Produkt,
    s.DecimalTotal          AS Saldo,
    s.DecimalReserved       AS Reserverat,
    s.DecimalAvailable      AS Tillgangligt,
    s.MoneyTotal            AS Lagervarde,
    CAST(s.DateLastModifiedUtc AT TIME ZONE 'UTC' AT TIME ZONE 'W. Europe Standard Time' AS DATETIME2(0)) AS SenastAndrad
FROM sitoo.StockItems s
LEFT JOIN sitoo.Warehouses w ON w.SiteId = s.SiteId AND w.WarehouseId = s.WarehouseId
LEFT JOIN sitoo.Stores    st ON st.SiteId = w.SiteId AND st.StoreId = w.StoreId
LEFT JOIN sitoo.Products   p ON p.SiteId = s.SiteId AND p.Sku = s.Sku AND p.IsDeleted = 0;
GO

CREATE OR ALTER VIEW sitoo.vOrderrader
AS
SELECT
    o.OrderId,
    o.EshopId,
    CAST(o.OrderDateUtc AT TIME ZONE 'UTC' AT TIME ZONE 'W. Europe Standard Time' AS DATETIME2(0)) AS Orderdatum,
    o.OrderTypeId,
    o.OrderStateId,
    o.RegisterId,
    o.WarehouseId,
    w.Name                  AS Lager,
    st.Name                 AS Butik,
    o.CurrencyCode,
    i.LineNumber            AS Radnummer,
    i.Sku,
    i.ProductName           AS Produkt,
    i.Quantity              AS Antal,
    i.DecimalQuantityTotal  AS AntalDecimal,
    i.MoneyNetPricePerUnit  AS PrisPerStyckExklMoms,
    i.MoneyDiscount         AS Rabatt,
    i.MoneyItemTotalNet     AS RadsummaExklMoms,
    i.MoneyItemTotalVat     AS RadMoms,
    i.VatValue              AS Momssats
FROM sitoo.Orders o
JOIN sitoo.OrderItems i ON i.OrderId = o.OrderId
LEFT JOIN sitoo.Warehouses w ON w.SiteId = o.EshopId AND w.WarehouseId = o.WarehouseId
LEFT JOIN sitoo.Stores    st ON st.SiteId = w.SiteId AND st.StoreId = w.StoreId;
GO

CREATE OR ALTER VIEW sitoo.vDagsforsaljning
AS
-- Försäljning per dag i svensk tid (förberäknad i sitoo.DailySales).
SELECT Dag, Forsaljning, ForsaljningInklMoms, Kvitton,
       Artiklar, ReaArtiklar, AndelRea, ArReadag
FROM sitoo.DailySales;
GO

-- Första gången, eller när rea-kolumnerna är nya: räkna om alla dagar.
IF EXISTS (SELECT 1 FROM sitoo.Orders)
   AND (NOT EXISTS (SELECT 1 FROM sitoo.DailySales) OR EXISTS (SELECT 1 FROM sitoo.DailySales WHERE ArReadag IS NULL))
    EXEC sitoo.RefreshDailySales;
GO

------------------------------------------------------------------------------
-- Tidrapport: manuella stämplingar och borttagna stämplingar
------------------------------------------------------------------------------
IF OBJECT_ID(N'sitoo.TimeStampAdd', N'U') IS NULL
CREATE TABLE sitoo.TimeStampAdd (
    Id          INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_TimeStampAdd PRIMARY KEY,
    Namn        NVARCHAR(255) NOT NULL,
    Stampling   DATETIME2(0)  NOT NULL,   -- svensk tid
    Kommentar   NVARCHAR(500) NULL,
    CreatedUtc  DATETIME2(0)  NOT NULL CONSTRAINT DF_TimeStampAdd_Created DEFAULT SYSUTCDATETIME()
);
GO
IF OBJECT_ID(N'sitoo.TimeStampRemove', N'U') IS NULL
CREATE TABLE sitoo.TimeStampRemove (
    OrderId     BIGINT        NOT NULL CONSTRAINT PK_TimeStampRemove PRIMARY KEY,
    Kommentar   NVARCHAR(500) NULL,
    CreatedUtc  DATETIME2(0)  NOT NULL CONSTRAINT DF_TimeStampRemove_Created DEFAULT SYSUTCDATETIME()
);
GO
-- Stämplingar som flyttats från en Sitoo-order till en annan person pekar på ordern,
-- så att de tas bort igen om orderns stämpling återställs.
IF COL_LENGTH(N'sitoo.TimeStampAdd', N'FranOrderId') IS NULL
    ALTER TABLE sitoo.TimeStampAdd ADD FranOrderId BIGINT NULL;
GO

------------------------------------------------------------------------------
-- Bokföring: datumrättning av z-rapporter och bankningar av kontanter
------------------------------------------------------------------------------
IF OBJECT_ID(N'sitoo.ZReportDateFix', N'U') IS NULL
CREATE TABLE sitoo.ZReportDateFix (
    RegisterId  NVARCHAR(100) NOT NULL,
    ZReportId   INT           NOT NULL,
    NyttDatum   DATE          NOT NULL,
    Kommentar   NVARCHAR(500) NULL,
    CreatedUtc  DATETIME2(0)  NOT NULL CONSTRAINT DF_ZReportDateFix_Created DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_ZReportDateFix PRIMARY KEY (RegisterId, ZReportId)
);
GO
IF OBJECT_ID(N'sitoo.BankDeposit', N'U') IS NULL
CREATE TABLE sitoo.BankDeposit (
    Id          INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_BankDeposit PRIMARY KEY,
    Datum       DATE          NOT NULL,
    Belopp      DECIMAL(19,2) NOT NULL,
    Kommentar   NVARCHAR(500) NULL,
    CreatedUtc  DATETIME2(0)  NOT NULL CONSTRAINT DF_BankDeposit_Created DEFAULT SYSUTCDATETIME()
);
GO

-- Z-rapporter med bokföringsdatum: rättat datum om det finns, annars dagen då
-- kassan öppnades (svensk tid), som i Sitoos utskrivna z-rapport.
CREATE OR ALTER VIEW sitoo.vZRapporter
AS
SELECT
    z.RegisterId, z.ZReportId, z.RegisterNumber, z.StoreName, z.EshopId,
    COALESCE(f.NyttDatum,
             CAST(CAST(COALESCE(z.DateOpenedUtc, z.DateCreatedUtc) AT TIME ZONE 'UTC'
                       AT TIME ZONE 'W. Europe Standard Time' AS DATETIME2(0)) AS date)) AS Dag,
    CAST(CAST(COALESCE(z.DateOpenedUtc, z.DateCreatedUtc) AT TIME ZONE 'UTC'
              AT TIME ZONE 'W. Europe Standard Time' AS DATETIME2(0)) AS date) AS OriginalDag,
    f.NyttDatum,
    CAST(CAST(z.DateCreatedUtc AT TIME ZONE 'UTC' AT TIME ZONE 'W. Europe Standard Time' AS DATETIME2(0)) AS DATETIME2(0)) AS Stangd,
    z.MoneySalesTotal, z.MoneyRefundTotal, z.MoneyRoundOff, z.MoneySummaryTotal,
    z.NumReceipts, z.Staff
FROM sitoo.ZReports z
LEFT JOIN sitoo.ZReportDateFix f ON f.RegisterId = z.RegisterId AND f.ZReportId = z.ZReportId;
GO

------------------------------------------------------------------------------
-- Worldline (Bambora): uppladdade månads- och utbetalningsrapporter
------------------------------------------------------------------------------
IF OBJECT_ID(N'sitoo.WorldlineFiles', N'U') IS NULL
CREATE TABLE sitoo.WorldlineFiles (
    Id            INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_WorldlineFiles PRIMARY KEY,
    FileName      NVARCHAR(260) NOT NULL,
    ReportType    NVARCHAR(20)  NOT NULL,      -- manad eller utbetalning
    Period        NVARCHAR(40)  NULL,
    Reference     NVARCHAR(40)  NULL,
    FileHash      CHAR(64)      NOT NULL,
    Transactions  INT           NOT NULL,
    NewRows       INT           NOT NULL,
    UploadedUtc   DATETIME2(0)  NOT NULL CONSTRAINT DF_WorldlineFiles_Uploaded DEFAULT SYSUTCDATETIME()
);
GO
IF OBJECT_ID(N'sitoo.WorldlineTransactions', N'U') IS NULL
CREATE TABLE sitoo.WorldlineTransactions (
    TransactionRef   NVARCHAR(40)  NOT NULL,
    Typ              NVARCHAR(30)  NOT NULL,   -- Sale, Refund ...
    TransactionDate  DATE          NOT NULL,
    TransactionTime  TIME(0)       NULL,
    CustomerNumber   NVARCHAR(20)  NULL,
    Merchant         NVARCHAR(100) NULL,       -- säljställe
    PaymentMethod    NVARCHAR(40)  NULL,
    CardType         NVARCHAR(60)  NULL,
    IssuerCountry    NVARCHAR(60)  NULL,
    Domain           NVARCHAR(30)  NULL,
    TerminalId       NVARCHAR(40)  NULL,
    Status           NVARCHAR(30)  NULL,
    Currency         NVARCHAR(3)   NULL,
    Amount           DECIMAL(19,2) NOT NULL,   -- belopp, returer negativa
    Tip              DECIMAL(19,2) NULL,
    Fee              DECIMAL(19,2) NULL,       -- negativ; NULL tills utbetalningen är uppladdad
    NetAmount        DECIMAL(19,2) NULL,
    PayoutRef        NVARCHAR(40)  NULL,
    PayoutDate       DATE          NULL,
    FileId           INT           NULL,
    UpdatedUtc       DATETIME2(0)  NOT NULL CONSTRAINT DF_WorldlineTransactions_Updated DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_WorldlineTransactions PRIMARY KEY (TransactionRef, Typ)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_WorldlineTransactions_Date')
    CREATE INDEX IX_WorldlineTransactions_Date ON sitoo.WorldlineTransactions (TransactionDate)
        INCLUDE (Amount, Fee, Merchant, Status, PayoutRef);
GO
IF OBJECT_ID(N'sitoo.WorldlinePayouts', N'U') IS NULL
CREATE TABLE sitoo.WorldlinePayouts (
    PayoutRef       NVARCHAR(40)  NOT NULL CONSTRAINT PK_WorldlinePayouts PRIMARY KEY,
    PayoutDate      DATE          NOT NULL,
    CustomerNumber  NVARCHAR(20)  NULL,
    Merchant        NVARCHAR(100) NULL,
    Account         NVARCHAR(40)  NULL,
    Transactions    INT           NULL,
    Currency        NVARCHAR(3)   NULL,
    Turnover        DECIMAL(19,2) NOT NULL,
    Fees            DECIMAL(19,2) NOT NULL,
    Adjustments     DECIMAL(19,2) NOT NULL,
    Chargebacks     DECIMAL(19,2) NOT NULL,
    Amount          DECIMAL(19,2) NOT NULL,
    FileId          INT           NULL
);
GO
IF OBJECT_ID(N'sitoo.WorldlineAdjustments', N'U') IS NULL
CREATE TABLE sitoo.WorldlineAdjustments (
    PayoutRef  NVARCHAR(40)  NOT NULL,
    Detaljer   NVARCHAR(200) NOT NULL,
    Belopp     DECIMAL(19,2) NOT NULL,
    Datum      DATE          NOT NULL,
    Currency   NVARCHAR(3)   NULL,
    CONSTRAINT PK_WorldlineAdjustments PRIMARY KEY (PayoutRef, Detaljer, Belopp)
);
GO
IF OBJECT_ID(N'sitoo.WorldlineChargebacks', N'U') IS NULL
CREATE TABLE sitoo.WorldlineChargebacks (
    CaseId          NVARCHAR(60)  NOT NULL CONSTRAINT PK_WorldlineChargebacks PRIMARY KEY,
    PayoutRef       NVARCHAR(40)  NULL,
    Datum           DATE          NULL,
    TransactionRef  NVARCHAR(40)  NULL,
    Reason          NVARCHAR(300) NULL,
    Belopp          DECIMAL(19,2) NOT NULL,
    Currency        NVARCHAR(3)   NULL
);
GO

-- Sparar en uppladdad rapport. Samma transaktion kan finnas i både månads- och
-- utbetalningsrapporten; den sparas en gång och kompletteras med det som saknas.
CREATE OR ALTER PROCEDURE sitoo.MergeWorldline
    @FileId INT,
    @Json   NVARCHAR(MAX)
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;
    DECLARE @actions TABLE (A NVARCHAR(10));
    BEGIN TRAN;

    MERGE sitoo.WorldlineTransactions AS t
    USING (
        SELECT * FROM OPENJSON(@Json, '$.transactions') WITH (
            TransactionRef NVARCHAR(40) '$.ref', Typ NVARCHAR(30) '$.typ', TransactionDate DATE '$.date',
            TransactionTime TIME(0) '$.time', CustomerNumber NVARCHAR(20) '$.customer', Merchant NVARCHAR(100) '$.merchant',
            PaymentMethod NVARCHAR(40) '$.method', CardType NVARCHAR(60) '$.cardtype', IssuerCountry NVARCHAR(60) '$.country',
            Domain NVARCHAR(30) '$.domain', TerminalId NVARCHAR(40) '$.terminal', Status NVARCHAR(30) '$.status',
            Currency NVARCHAR(3) '$.currency', Amount DECIMAL(19,2) '$.amount', Tip DECIMAL(19,2) '$.tip',
            Fee DECIMAL(19,2) '$.fee', NetAmount DECIMAL(19,2) '$.net', PayoutRef NVARCHAR(40) '$.payoutref',
            PayoutDate DATE '$.payoutdate')
    ) AS s ON t.TransactionRef = s.TransactionRef AND t.Typ = s.Typ
    WHEN MATCHED THEN UPDATE SET
        TransactionDate = s.TransactionDate,
        TransactionTime = COALESCE(t.TransactionTime, s.TransactionTime),
        CustomerNumber = COALESCE(s.CustomerNumber, t.CustomerNumber),
        Merchant = COALESCE(s.Merchant, t.Merchant),
        PaymentMethod = COALESCE(s.PaymentMethod, t.PaymentMethod),
        CardType = COALESCE(s.CardType, t.CardType),
        IssuerCountry = COALESCE(s.IssuerCountry, t.IssuerCountry),
        Domain = COALESCE(s.Domain, t.Domain),
        TerminalId = COALESCE(s.TerminalId, t.TerminalId),
        Status = COALESCE(s.Status, t.Status),
        Currency = COALESCE(s.Currency, t.Currency),
        Amount = s.Amount,
        Tip = COALESCE(s.Tip, t.Tip),
        Fee = COALESCE(s.Fee, t.Fee),
        NetAmount = COALESCE(s.NetAmount, t.NetAmount),
        PayoutRef = COALESCE(s.PayoutRef, t.PayoutRef),
        PayoutDate = COALESCE(s.PayoutDate, t.PayoutDate),
        FileId = @FileId,
        UpdatedUtc = SYSUTCDATETIME()
    WHEN NOT MATCHED THEN INSERT (TransactionRef, Typ, TransactionDate, TransactionTime, CustomerNumber, Merchant,
        PaymentMethod, CardType, IssuerCountry, Domain, TerminalId, Status, Currency, Amount, Tip, Fee, NetAmount,
        PayoutRef, PayoutDate, FileId)
        VALUES (s.TransactionRef, s.Typ, s.TransactionDate, s.TransactionTime, s.CustomerNumber, s.Merchant,
        s.PaymentMethod, s.CardType, s.IssuerCountry, s.Domain, s.TerminalId, s.Status, s.Currency, s.Amount, s.Tip,
        s.Fee, s.NetAmount, s.PayoutRef, s.PayoutDate, @FileId)
    OUTPUT $action INTO @actions;

    MERGE sitoo.WorldlinePayouts AS t
    USING (
        SELECT * FROM OPENJSON(@Json, '$.payouts') WITH (
            PayoutRef NVARCHAR(40) '$.ref', PayoutDate DATE '$.date', CustomerNumber NVARCHAR(20) '$.customer',
            Merchant NVARCHAR(100) '$.merchant', Account NVARCHAR(40) '$.account', Transactions INT '$.transactions',
            Currency NVARCHAR(3) '$.currency', Turnover DECIMAL(19,2) '$.turnover', Fees DECIMAL(19,2) '$.fees',
            Adjustments DECIMAL(19,2) '$.adjustments', Chargebacks DECIMAL(19,2) '$.chargebacks',
            Amount DECIMAL(19,2) '$.amount')
    ) AS s ON t.PayoutRef = s.PayoutRef
    WHEN MATCHED THEN UPDATE SET PayoutDate = s.PayoutDate, CustomerNumber = COALESCE(s.CustomerNumber, t.CustomerNumber),
        Merchant = COALESCE(s.Merchant, t.Merchant), Account = COALESCE(s.Account, t.Account),
        Transactions = COALESCE(s.Transactions, t.Transactions), Currency = COALESCE(s.Currency, t.Currency),
        Turnover = s.Turnover, Fees = s.Fees, Adjustments = s.Adjustments, Chargebacks = s.Chargebacks,
        Amount = s.Amount, FileId = @FileId
    WHEN NOT MATCHED THEN INSERT (PayoutRef, PayoutDate, CustomerNumber, Merchant, Account, Transactions, Currency,
        Turnover, Fees, Adjustments, Chargebacks, Amount, FileId)
        VALUES (s.PayoutRef, s.PayoutDate, s.CustomerNumber, s.Merchant, s.Account, s.Transactions, s.Currency,
        s.Turnover, s.Fees, s.Adjustments, s.Chargebacks, s.Amount, @FileId);

    MERGE sitoo.WorldlineAdjustments AS t
    USING (
        SELECT PayoutRef, Detaljer, Belopp, MAX(Datum) AS Datum, MAX(Currency) AS Currency
        FROM OPENJSON(@Json, '$.adjustments') WITH (
            PayoutRef NVARCHAR(40) '$.payoutref', Detaljer NVARCHAR(200) '$.details', Belopp DECIMAL(19,2) '$.amount',
            Datum DATE '$.date', Currency NVARCHAR(3) '$.currency')
        GROUP BY PayoutRef, Detaljer, Belopp
    ) AS s ON t.PayoutRef = s.PayoutRef AND t.Detaljer = s.Detaljer AND t.Belopp = s.Belopp
    WHEN MATCHED THEN UPDATE SET Datum = s.Datum, Currency = s.Currency
    WHEN NOT MATCHED THEN INSERT (PayoutRef, Detaljer, Belopp, Datum, Currency)
        VALUES (s.PayoutRef, s.Detaljer, s.Belopp, s.Datum, s.Currency);

    MERGE sitoo.WorldlineChargebacks AS t
    USING (
        SELECT * FROM OPENJSON(@Json, '$.chargebacks') WITH (
            CaseId NVARCHAR(60) '$.caseid', PayoutRef NVARCHAR(40) '$.payoutref', Datum DATE '$.date',
            TransactionRef NVARCHAR(40) '$.ref', Reason NVARCHAR(300) '$.reason', Belopp DECIMAL(19,2) '$.amount',
            Currency NVARCHAR(3) '$.currency')
    ) AS s ON t.CaseId = s.CaseId
    WHEN MATCHED THEN UPDATE SET PayoutRef = s.PayoutRef, Datum = s.Datum, TransactionRef = s.TransactionRef,
        Reason = s.Reason, Belopp = s.Belopp, Currency = s.Currency
    WHEN NOT MATCHED THEN INSERT (CaseId, PayoutRef, Datum, TransactionRef, Reason, Belopp, Currency)
        VALUES (s.CaseId, s.PayoutRef, s.Datum, s.TransactionRef, s.Reason, s.Belopp, s.Currency);

    DECLARE @new INT = (SELECT COUNT(*) FROM @actions WHERE A = 'INSERT');
    UPDATE sitoo.WorldlineFiles SET NewRows = @new WHERE Id = @FileId;
    COMMIT;
    SELECT @new AS NewRows, (SELECT COUNT(*) FROM @actions) AS Rows;
END
GO
