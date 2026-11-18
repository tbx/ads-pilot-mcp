# ads-pilot-mcp: Positionierung

Stand: 18.11.2026 · English version: [positioning.md](positioning.md)

## In einem Satz

**Google Ads über Claude lenken, mit Leitplanken für Agenturen.**

## Für wen

Agenturen, Berater und Inhouse-Teams, die mehrere Google-Ads-Konten betreuen, oft über ein Verwaltungskonto (MCC), und die Claude echte Arbeit im Konto überlassen wollen, ohne die Kontrolle abzugeben.

## Das Problem

Wer mit Claude in Google Ads arbeitet, hat heute die Wahl zwischen:

- **Gehosteten Diensten.** Sie können viel, aber Zugangsdaten und Kontodaten laufen über einen fremden Server.
- **Großen Open-Source-Servern.** Sie decken viele Plattformen mit sehr vielen Werkzeugen ab. Das ist mächtig, aber schwer zu überblicken und schwer zu prüfen.
- **Nur Auswerten.** Das ist sicher, aber jede Änderung bleibt Handarbeit in der Weboberfläche.

Für Agenturen kommt ein Risiko dazu, das kaum ein Werkzeug gezielt abfängt: **die Änderung im falschen Kundenkonto.** Über ein MCC hat ein Login Zugriff auf alle Kunden, und die Google-Ads-API hält eine Änderung im falschen Konto nicht auf.

## Die Antwort

| Prinzip | Was das konkret heißt |
|---|---|
| **Klein** | Wenige Werkzeuge für die tägliche Kontobetreuung. Claude wählt zuverlässig das richtige aus, und der Kontext bleibt frei für die eigentliche Arbeit. |
| **Lokal** | Läuft auf dem eigenen Rechner, mit dem eigenen Google-Cloud-Projekt. Kein Dienst dazwischen, keine Daten bei Dritten. |
| **Nachprüfbar** | Der ganze Code ist in einer Stunde gelesen. Wer ihn einsetzt, weiß, was er tut. |
| **Leitplanken** | Probelauf vor jeder Änderung, Freigabeliste pro Profil, kein Löschen, Protokoll mit altem und neuem Wert. |

## Die Leitplanken im Detail

1. **Probelauf ist Standard.** Jede Änderung wird zuerst nur geprüft und zeigt alten und neuen Wert. Ausgeführt wird erst nach ausdrücklicher Bestätigung.
2. **Freigabeliste pro Profil.** Schreibzugriff gibt es nur auf Konten, die im Profil stehen. Claude kann diese Liste nicht ändern.
3. **Ein Profil pro Kunde.** Das Profil wird beim Start festgelegt. Jede Antwort nennt Profil und Kunden-ID.
4. **Kein Löschen.** Nur pausieren und aktivieren. Das lässt sich immer zurücknehmen.
5. **Keine heiklen Bereiche.** Keine Abrechnung, Nutzer oder Kontoeinstellungen. Kampagnenbudgets nur bis zu einer Obergrenze pro Profil und in begrenzten Schritten.
6. **Lückenloses Protokoll.** Jede ausgeführte Änderung mit Zeit, Profil, Konto, Objekt, altem und neuem Wert. So lässt sie sich gegenüber dem Kunden belegen und bei Bedarf von Hand zurücknehmen.

## Was es kann

**Version 1: Optimieren** (in Arbeit)

- Konten auflisten, freie Abfragen (GAQL)
- Keywords, Anzeigen und Anzeigengruppen pausieren oder aktivieren
- Keywords zu Anzeigengruppen hinzufügen
- Gebotsanpassungen für Geräte und Uhrzeiten (Werbezeitplaner)
- Finale URL, Titel und Texte von Responsive Search Ads ändern
- Tagesbudgets von Kampagnen, innerhalb der Grenzen im Profil

**Ausblick**

| Version | Rolle von Claude | Inhalt |
|---|---|---|
| 2: Aufbauen | Kampagnenmanager | Suchkampagnen komplett anlegen. Neue Kampagnen starten pausiert, Budgets nur beim Anlegen und nur bis zu einer Obergrenze pro Profil. |
| 3: Erweitern | Spezialist | Weitere Kampagnentypen, Assets, Keyword-Recherche |
| 4: Begleiten | Account Manager | Wiederkehrende Kontrollen und Berichte. Claude schlägt vor, der Mensch gibt frei. |

**Option in jeder Stufe: der eigene Server.** Heute startet Claude Desktop ads-pilot lokal auf dem Mac. Auf einem eigenen Server (HTTPS, mit Anmeldung) wird daraus ein benutzerdefinierter Konnektor in Claude: nutzbar in claude.ai im Browser, in der Mobile-App und von Kollegen, und die Grundlage für regelmäßige Prüfungen in Version 4. Es bleibt selbst betrieben, mit dem eigenen Google-Cloud-Projekt.

Die Leitplanken gelten in jeder Version und in jeder Betriebsart.

## Was es bewusst nicht ist

- **Kein gehosteter Dienst.** Jeder Nutzer verwendet sein eigenes Google-Cloud-Projekt und betreibt ads-pilot auf dem eigenen Mac oder Server.
- **Kein Alleskönner für alle Werbeplattformen.** Es geht nur um Google Ads, das aber gründlich.
- **Kein Autopilot.** Claude schlägt vor und setzt um, der Mensch gibt frei.

## Einordnung im Markt

Ohne Wertung, als Orientierung:

- **Googles eigener Google Ads MCP Server** ([googleads/google-ads-mcp](https://github.com/googleads/google-ads-mcp)) ist sehr gut für Auswertungen. Er schreibt bisher nur Assets und Labels.
- **Gehostete Dienste** im Claude-Verzeichnis, etwa Adspirer, AdCopilot oder Windsor.ai, bieten viele Plattformen und viele Funktionen. Die Konten laufen dabei über den jeweiligen Anbieter.
- **Umfangreiche Open-Source-Server** wie [AdLoop](https://github.com/kLOsk/adloop) sind lokal und vielseitig, decken mehrere Plattformen ab und entwickeln sich schnell.

ads-pilot-mcp setzt bewusst auf weniger Umfang, auf Ruhe statt Tempo und auf Schutz vor Änderungen im falschen Konto.

## Texte zum Verwenden

**GitHub-Beschreibung (Englisch, eine Zeile)**

> Local MCP server for Google Ads with dry runs, an account allowlist and a change log.

**Mini-Intro für die README**

> **ads-pilot-mcp** lässt Claude in Google-Ads-Konten arbeiten, ohne die Weboberfläche zu bedienen: auswerten, Keywords und Anzeigen pausieren oder aktivieren, Keywords ergänzen, Gebote nach Gerät und Uhrzeit anpassen, Anzeigentexte überarbeiten.
>
> Gebaut für alle, die mehrere Kundenkonten betreuen. Jede Änderung startet als Probelauf mit altem und neuem Wert. Ausgeführt wird erst nach Bestätigung und nur in Konten, die im Profil freigegeben sind. Alles landet in einem Änderungsprotokoll. Budgets ändern sich nur innerhalb der Grenzen, die Sie pro Profil festlegen; Löschen und Abrechnung sind bewusst nicht möglich.
>
> Klein und lokal: Der Server läuft auf dem eigenen Rechner mit dem eigenen Google-Cloud-Projekt, und der Code ist schnell gelesen.
>
> Für reine Auswertungen empfiehlt sich auch Googles eigener [Google Ads MCP Server](https://github.com/googleads/google-ads-mcp).

## Lizenz und Geschäftsmodell

**Open Source unter AGPL-3.0, kommerzielle Lizenz auf Anfrage.**

- Agenturen, Berater und Inhouse-Teams nutzen ads-pilot kostenlos, für sich und ihre Kunden.
- Wer ein verändertes ads-pilot als Dienst im Netz anbietet, muss seine Änderungen offenlegen. Niemand kann daraus still einen geschlossenen Bezahldienst bauen.
- Für geschlossene Produkte oder gehostete Dienste gibt es eine kommerzielle Lizenz vom Urheber (Dual Licensing).
- Der Name „ads-pilot“ bleibt beim Urheber. Abwandlungen brauchen einen anderen Namen.

Vergleichbare Projekte nutzen fast alle MIT (Googles eigener Server: Apache 2.0). AdLoop etwa steht unter MIT und verdient mit einer gehosteten Cloud-Version. ads-pilot startet mit dem stärkeren Schutz und kann später noch lockern; umgekehrt geht das nicht.

## Offene Punkte

- README auf Englisch, mit deutscher Fassung der werblichen Texte.
- Vor der Veröffentlichung den Marktüberblick erneut prüfen, weil sich die Projekte schnell entwickeln.
- CLA-Text aufsetzen, bevor der erste Beitrag von außen angenommen wird.
- Kontakt für Anfragen zur kommerziellen Lizenz festlegen (Website oder E-Mail).
