# Crypto Paper AI

En liten paper-trading-miljo for att lata en enkel AI-agent folja kryptopriser,
gora fiktiva sma transaktioner och lara sig av utfallet over tid.

Det har avsiktligt ingen kod for riktiga order eller API-nycklar. Syftet ar att
samla data, testa beteenden och fa fram rapporter innan man ens diskuterar
live-handel.

## Vad den gor

- Hamter publika topp-50 marknadsdata fran CoinGecko for att fungera stabilt
  pa GitHub Actions.
- Sparar priser, fiktiva affarer, portfolj och modellvikter i SQLite.
- Borjar med 100 fiktiva USDT enligt `config.example.json`.
- Gor sma kop/salj/hall-beslut med en enkel online-modell.
- Blandar in minimal utforskning, men slumpen far inte oppna nya positioner.
- Skannar topp 20 kryptomarknader efter handelsvolym, market cap och minsta
  volym.
- Lagrar pris, 24h-volym, 24h-forandring samt high/low for varje snapshot.
- Lagger till features for momentum, volatilitet, RSI, volymforandring och
  var priset ligger i sitt 24h-intervall.
- Sparar varje beslut med action, confidence, orsak och features.
- Utvarderar beslut efter 5, 15 och 60 minuter mot BTC som enkel benchmark.
- Tranar modellen vidare pa utvarderade 15-minutersbeslut.
- Har riskregler for max 2 oppna positioner, 4% portfoljvarde per position,
  stop-loss pa -5% och take-profit.
- Pausar nya kop om paper-portfoljen faller mer an den tillatna drawdown-gransen.
- Fortsatter bevaka oppna positioner aven om de tillfalligt hamnar utanfor topp
  50-listan.

## Starta

Krav: Python 3.10 eller nyare. Inga externa paket kravs.

```powershell
cd "C:\Users\kjell\OneDrive\Dokument\crypto-paper-ai"
powershell -ExecutionPolicy Bypass -File .\run.ps1 --once
```

Kor den kontinuerligt:

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1
```

Visa portfolj:

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1 --summary
```

Visa handels- och larrapport:

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1 --report
```

I rapporten betyder:

- `decision_count`: hur manga beslut som sparats.
- `evaluated_decisions`: hur manga beslut som hunnit fa facit efter 5/15/60 min.
- `trained_evaluations`: hur manga facit som anvants for extra traning.
- `outcomes_by_horizon_seconds`: om besluten slog eller tappade mot BTC efter
  300, 900 och 3600 sekunder.
- `action_outcomes`: om BUY, SELL och HOLD blev bra eller daliga per tidsfonster.

## Konfiguration

Kopiera `config.example.json` om du vill experimentera:

```powershell
Copy-Item config.example.json config.local.json
powershell -ExecutionPolicy Bypass -File .\run.ps1 --config config.local.json
```

Viktiga falt:

- `starting_cash`: fiktivt startkapital.
- `position_fraction`: hur stor del av portfoljen en ny position far vara.
- `max_open_positions`: max antal oppna positioner samtidigt.
- `exploration_rate`: hur ofta agenten testar slumpmassiga beslut for att lara sig.
- `learning_rate`: hur snabbt modellen justerar sig efter nya prisrorelser.
- `symbols`: CoinGecko-id och kort symbol for marknader att bevaka.
- `universe.top_n`: hur manga marknader som ska skannas.
- `risk.max_drawdown_pause_pct`: nar nya kop ska pausas efter for stor nedgang.

## Vagen mot live-handel

Innan riktiga pengar finns med bor systemet byggas ut med:

1. Backtesting pa historisk data och rapporter for drawdown, avgifter och risk.
2. Separat strategiutvardering med walk-forward-testning.
3. Harda riskgransar: dagsforlust, max orderstorlek, max exponering och manuell nodbroms.
4. Exchange-integration i sandbox/testnet forst.
5. Manuell godkannandeloop dar systemet foreslar order men inte skickar dem.
6. Slutligen live-handel med mycket sma belopp och loggning av allt.

Det har ar inte finansiell radgivning. Kryptomarknader ar volatila och en modell
som fungerar i test kan forlora pengar i verkligheten.

## Kora gratis pa GitHub

Projektet innehaller en GitHub Actions-workflow i
`.github/workflows/paper-trader.yml`.

Nar projektet ligger i ett GitHub-repo kor workflowen:

- automatiskt var 5:e minut
- manuellt via fliken **Actions** och **Run workflow**
- `python paper_ai.py --cycles 5` for fem beslut med 60 sekunder mellan varje
  beslut
- `python paper_ai.py --summary` efterat
- `python paper_ai.py --report` efterat

Databasen `paper_trader.sqlite3` sparas mellan korningar med GitHub Actions
cache och laddas dessutom upp som artifact i 30 dagar sa du kan granska den.

Kort start:

```powershell
cd "C:\Users\kjell\OneDrive\Dokument\crypto-paper-ai"
git init
git add .
git commit -m "Add crypto paper trading AI"
```

Skapa sedan ett nytt repo pa GitHub och folj instruktionerna dar for att pusha
projektet.

Obs: GitHub Actions kan inte starta schemalagda workflows exakt varje minut.
GitHubs kortaste schemaintervall ar 5 minuter, sa workflowen kor fem
1-minuterscykler inne i varje jobb.
