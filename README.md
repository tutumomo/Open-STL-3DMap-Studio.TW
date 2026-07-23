# Open STL-3DMap Studio

[![Build Windows installer](https://github.com/OpenAEC-Foundation/Open-STL-3DMap-Studio/actions/workflows/build-release.yml/badge.svg)](https://github.com/OpenAEC-Foundation/Open-STL-3DMap-Studio/actions/workflows/build-release.yml)

**Download:** de nieuwste installer en portable ZIP staan bij de
[releases](https://github.com/OpenAEC-Foundation/Open-STL-3DMap-Studio/releases/latest).

Maakt van elk stukje Nederland een meerkleuren 3D-kaart voor bijvoorbeeld de
Creality K1 Max met CFS. Je prikt een locatie aan op de kaart, sleept een vlak,
verbergt de gebouwen die weg moeten, zet er eventueel je eigen Revit/IFC-ontwerp
voor in de plaats, en krijgt er een 3MF plus losse STL's uit.

Gebouwhoogtes komen uit **3DBAG** (TU Delft, ingemeten met AHN-laserdata), niet
uit geschatte OSM-tags. Water, wegen, groen en bomen komen uit **OpenStreetMap**,
adreszoeken en de achtergrondkaart uit **PDOK**.

Een project van de [OpenAEC Foundation](https://github.com/OpenAEC-Foundation).
De interface volgt de [OpenAEC Style Book](https://github.com/OpenAEC-Foundation/OpenAEC-style-book).

## Wat het kan

- **Water, wegen, land, gebouwen** — de basiskaart, elk in een eigen kleur-Z-band.
- **Bomen en struiken** — losse bomen als kegeltjes, bos en struweel als verhoogde
  vlakken (aan te zetten onder *Lagen*).
- **Gedetailleerde daken (LoD2.2)** — echte dakvormen i.p.v. blokken. Langzaam,
  dus optioneel.
- **Eigen ontwerp** — IFC/STL/OBJ op maaiveldhoogte in de kaart plaatsen.
- **Filamentsloten zelf toewijzen** — per laag, met een waarschuwing zodra je er
  meer dan 4 gebruikt (de CFS-C heeft 4 sloten).
- **Automatische update-check** en een **feedbackknop** naar de GitHub-issues.

## Installeren

Dubbelklik **`packaging\Output\OpenSTL-3DMapStudio-1.1.0-Setup.exe`**.

De installatie gaat standaard naar je eigen gebruikersprofiel, dus je krijgt
geen UAC-prompt. In de installer kun je alsnog voor "alle gebruikers" kiezen.
Python hoef je niet te hebben — dat zit in de bundel, ook op een andere pc.

Na installatie staat het programma in het Startmenu. Verwijderen gaat via
Instellingen → Apps, of via de snelkoppeling in het Startmenu.

Je hebt internet nodig: alle kaartdata wordt live opgehaald. Wat eenmaal is
opgehaald blijft gecachet in `%LOCALAPPDATA%\Open STL-3DMap Studio\cache`.

### Vanuit de broncode draaien

Voor sleutelen aan de code:

```
install.bat     (eenmalig - maakt een venv met Python 3.12)
start.bat
```

Vereist [uv](https://docs.astral.sh/uv/):
`powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`

### De installer opnieuw bouwen

```
build_installer.bat
```

Genereert het icoon, bundelt met PyInstaller en compileert de setup met Inno
Setup (`winget install --id JRSoftware.InnoSetup`).

## Werkwijze

1. **Locatie** — zoek een adres of plaats.
2. **Gebied** — teken een vlak, of vul breedte/hoogte in meters in en klik
   "Vlak om midden van kaart". Klik dan "Kaartdata ophalen".
3. **Gebouwen verbergen** — klik gebouwen aan, of sleep een vak. Verborgen
   gebouwen worden rood en komen niet in de STL.
4. **Eigen ontwerp** — upload een IFC (Revit → Bestand → Exporteren → IFC) of
   een STL/OBJ/3MF. Sleep de gele pin naar de juiste plek, stel rotatie en
   schaal in. Het ontwerp wordt automatisch op dezelfde schaal als de kaart
   gezet en op maaiveldhoogte geplaatst.
5. **Printinstellingen** — zie hieronder.
6. **Genereren** — je krijgt `<naam>.3mf` plus losse STL's.
   - **Genereren** schrijft naar de exportmap die bovenaan staat.
   - **Opslaan als…** opent een Windows-mapdialoog; die keuze wordt onthouden
     als nieuwe exportmap.
   - **Map openen in Verkenner** brengt je er direct naartoe.

   Standaard is dat `Documenten\3D Kaart Bouwer\<naam>\`.

## Waarom het zo min mogelijk filamentwissels kost

Bij multicolor is niet het aantal kleuren duur, maar het aantal *wissels*: elke
wissel purgt filament. Kleuren die in bovenaanzicht naast elkaar liggen kosten
twee wissels per laag — over een hele plaat loopt dat op tot kilo's afval.

Daarom krijgt elke kleur hier zijn eigen **Z-band**:

| mm        | Body            | CFS-slot | Wat je ziet                          |
|-----------|-----------------|----------|--------------------------------------|
| 0 – 1,2   | onderplaat      | 1        | water, door de gaten in het maaiveld |
| 1,2 – 1,8 | wegen           | 3        | straten, verdiept in het maaiveld    |
| 1,2 – 2,0 | maaiveld        | 2        | het land                             |
| 2,0 – top | gebouwen        | 4        | de skyline op echte AHN-hoogte       |

Alleen de band 1,2–1,8 heeft twee kleuren tegelijk. Voor een kaart van 200 mm
komt dat neer op ongeveer **8 filamentwissels voor de hele print**. De teller in
het scherm laat het exacte aantal zien voordat je exporteert.

Zet je "Wegen" op *verhoogd* in plaats van *verdiept*, dan steken de straten
boven het maaiveld uit; dat kost een paar wissels meer.

De onderplaat is altijd een volle plaat, ook als er geen water in beeld is. Hij
draagt alles en vormt de gekleurde rand rondom de kaart.

## Instellingen die ertoe doen

- **Max. formaat** — de langste zijde van de plaat. De K1 Max doet 300 × 300,
  dus tot ~280 mm werkt.
- **Laaghoogte** — alle bandgrenzen worden automatisch afgerond op hele lagen.
  Doe je dat niet, dan verschuift de slicer een kleurovergang stilletjes een
  laag en lijkt het model kapot.
- **Hoogte ×** — verticale overdrijving. Bij 1,0 zijn de gebouwen exact op
  schaal. Nederland is vlak, dus bij gebieden groter dan ~2 km is 2 tot 3 mooier.
- **Min. wegbreedte** — een 0,4 nozzle kan niets smallers dan ~0,8 mm. Wegen
  die op de gekozen schaal te smal uitvallen worden hiernaar verbreed; brede
  wegen houden hun echte breedte.
- **Groen** — parken als vijfde body. De CFS-C heeft maar 4 sloten, dus dit is
  standaard uit. Wil je het toch: geef groen hetzelfde slot als het maaiveld, of
  zet wegen uit.

## In de slicer (Creality Print / OrcaSlicer)

Open **`<naam>.3mf`**. Daar zitten alle onderdelen al in als losse parts, op de
juiste hoogte, met de CFS-sloten toegewezen — je hoeft niets uit te lijnen.

Het 3MF gebruikt de Bambu/Orca-indeling: elk onderdeel is een eigen object in
`3D/Objects/object-1.model`, samengebonden met `<components>`, en de
filamentsloten staan in `Metadata/model_settings.config`. Creality Print stamt
van Bambu Studio af en leest precies dat. (De PrusaSlicer-indeling, met
driehoekbereiken in `Slic3r_PE_model.config`, laadt in Creality Print als één
klomp zonder kleurtoewijzing — die valkuil is hier vermeden.)

### De losse STL's

Alleen nodig als het 3MF niet werkt. **Laad ze niet alle vier los in**: een
slicer legt elk object plat op het bed, waardoor de hele Z-stapeling instort en
alles op de basisplaat belandt.

De juiste volgorde:

1. Laad alleen `<naam>_1_base.stl`.
2. Rechtermuisknop op dat object → **Add part** / *Onderdeel toevoegen* → **Load**.
3. Kies daar de andere drie STL's.
4. Wijs per part het filamentslot toe — het cijfer in de bestandsnaam.

Als *part* houden de bestanden hun eigen hoogte; als los *object* niet. Bij
elke export komt een `<naam>_LEESMIJ.txt` te staan met deze stappen en de
gebruikte Z-hoogtes.

Verder:

- Zet **"flush into infill"** en **"flush into object"** aan. Dat scheelt het
  meeste purgemateriaal.
- Laagje van 0,2 mm werkt prima; de bandgrenzen zijn daarop afgestemd.
- Kies contrasterende kleuren. Water donkerblauw, maaiveld licht, wegen wit en
  gebouwen een warme tint leest het best.

## Databronnen en licenties

Deel je een print of foto, vermeld dan:

- **3DBAG** — TU Delft / 3DGI, [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)
- **OpenStreetMap** — © OpenStreetMap-bijdragers, [ODbL](https://www.openstreetmap.org/copyright)
- **PDOK / Kadaster** — achtergrondkaart, luchtfoto en adreszoeken

De code zelf staat onder de [MIT-licentie](LICENSE). De OpenAEC-huisstijl
(kleuren, typografie) komt uit de OpenAEC Style Book onder CC BY-SA 4.0.

## Bestanden

```
app/config.py         instellingen, Z-banden, projectstructuur
app/geo.py            RD-projectie en het omrekenen naar millimeters
app/paths.py          waar bestanden staan, geinstalleerd en in ontwikkeling
app/sources.py        3DBAG (WFS + API-fallback), Overpass, PDOK
app/meshing.py        polygonen naar prisma's, STL- en 3MF-export
app/ifc_import.py     IFC/STL/OBJ inlezen en op de kaart plaatsen
app/folder_dialog.py  de "opslaan als"-mapdialoog
app/pipeline.py       het geheel aan elkaar knopen
app/buildings3d.py    3DBAG LoD2.2 solids -> gedetailleerde daken
app/appinfo.py        naam, versie, repo-URL's
app/updater.py        controleert GitHub Releases op een nieuwe versie
app/main.py           webserver
run_app.py            startpunt van de gebundelde applicatie
web/                  de interface
packaging/            icoon, PyInstaller-spec, Inno Setup-script
smoketest.py          haalt een echt gebied op en exporteert het
verify_output.py      controleert het 3MF en of er niets zweeft
```

Waar het programma je gegevens bewaart, eenmaal geïnstalleerd:

| Wat | Waar |
|---|---|
| Gedownloade kaartdata | `%LOCALAPPDATA%\Open STL-3DMap Studio\cache` |
| Geüploade IFC-bestanden | `%LOCALAPPDATA%\Open STL-3DMap Studio\uploads` |
| Logbestand | `%LOCALAPPDATA%\Open STL-3DMap Studio\log.txt` |
| Gegenereerde modellen | `Documenten\Open STL-3DMap Studio` (of je eigen keuze) |

Verwijder je het programma, dan gaan alleen de cache en het log weg. Je
gegenereerde modellen blijven staan.

## Als er iets misgaat

- **Water/wegen niet opgehaald** — de publieke Overpass-servers (voor OSM-data)
  zijn soms druk. De tool probeert vijf mirrors en gaat desnoods door met alleen
  de gebouwen; klik daarna nog eens op *Kaartdata ophalen* om de rest te halen.
- **Ophalen duurt lang** — de eerste keer per gebied wel. Daarna komt het uit
  `cache\`. Die map mag je altijd weggooien.
- **Gebied te groot** — boven 16 km² stopt de tool. Op een plaat van 200 mm is
  een straat daar toch al smaller dan de nozzle aankan.
- **IFC laadt niet** — exporteer uit Revit als STL en upload die. In de tool
  staat `ifcopenshell`, maar niet elke IFC-variant komt er doorheen.

Draai `smoketest.py` en `verify_output.py` als je wilt controleren of de
pijplijn nog klopt na een wijziging.
