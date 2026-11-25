"""Build violations/data/declaration_catalog.json from the recorded Declaration of Covenants, Conditions and Restrictions (CC&Rs).

    pdftotext -layout CCRs.pdf ccr.txt
    python tools/build_declaration_catalog.py ccr.txt

The rule text is copied from the PDF's text layer (a scan, so it is OCR text) with the OCR slips I found corrected (see FIX). PROOFREAD any
section before relying on it in a hearing. Then `python manage.py load_standards` loads these beside the Community Standards.
Anything in [square brackets] in the drafts must be filled in by whoever issues the notice."""
import json
import re
import sys
from pathlib import Path

SOURCE = "Declaration of Covenants, Conditions and Restrictions for Heron Landing (recorded November 30, 2017, Instrument 2017146908)"
ARTICLES = {"5", "7", "8", "9", "10", "14"}
FIX = [("Seetion", "Section"), ("sueh", "such"), ("sneh", "such"), ("feneing", "fencing"), ("Reeeiver", "Receiver"), ("pareels", "parcels"),
       ("Tor Sale", "For Sale"), ("d'A feet", "four and one-half feet (4½')"), ("d’he", "The"), ("’‘", "“"), ("‘’", "”"), ("’’", "”"),
       ("Vll", "VII"), ("Xll", "XII"), ("Lof'", 'Lot"'), ("Seetion", "Section"), ("Reeeivers", "Receivers"), ("5*)", "5')"),
       ("W ithout", "Without"), ('"yard sales”', "“yard sales”")]

ALT, VEH, TRASH, LIGHT, SIGN, PETS, REC, LAND, MAINT, LEASE, WATER, OTHER = (
    "Alterations & approvals", "Vehicles & parking", "Trash, storage & clutter", "Lighting & decorations", "Signs, flags & events",
    "Pets & nuisance", "Recreation & play", "Landscaping & lawn", "Maintenance & appearance", "Leasing & rentals", "Drainage & water", "Other")

D = []


def add(code, section, title, category, days, keywords, observed, cure, start=None, end=None):
    """start / end: markers such as '(B)' that cut out the part of the section that is quoted (default: the whole section)."""
    D.append(dict(code=code, section=section, title=title, category=category, cure_days=days, keywords=keywords, observed=observed,
                  cure=cure, start=start, end=end))


add("D-5.2", "5.2", "Business use of a home", OTHER, 15, "business commercial home business trade profession customers traffic office",
    "A business or commercial activity ([describe]) is being conducted at the property, with [customer / delivery / vehicle traffic beyond that of an ordinary home].",
    "Stop the commercial activity, or bring it within the Declaration: a home business is allowed only if it complies with Sarasota County's home-business rules and "
    "generates no more pedestrian or vehicle traffic than an ordinary single-family home.")
add("D-5.3", "5.3", "Temporary structure, trailer or prefabricated building", ALT, 15, "tent trailer shack barn outbuilding prefabricated shed temporary structure residence living",
    "A [tent / trailer / shack / prefabricated structure / outbuilding] was observed at [location]. [It is being used as a residence / it is visible from neighboring property].",
    "Remove the structure. No temporary structure, trailer, tent, shack, barn or outbuilding may be used as a residence, and prefabricated structures visible from neighboring "
    "property require prior written approval.")
add("D-5.4", "5.4", "Sign without approval (including For Sale / For Lease)", SIGN, 7,
    "sign signs for sale for lease rent realtor banner advertisement political yard sign poster",
    "A [For Sale / For Lease / other] sign ([describe]) is displayed at [location], visible from neighboring property, with no written approval on file.",
    "Remove the sign, or obtain the Association's prior written approval. The only sign allowed without approval is one discreet, professionally prepared For Sale or "
    "For Lease sign no larger than 18 by 24 inches, for no more than 60 days, after written notice to the Association's Secretary. Lease or rent signs are not allowed during "
    "the first 24 months after the original purchase from the builder.")
add("D-5.5", "5.5", "Noxious or offensive activity / sound devices", PETS, 7, "noise loud music speakers horns sirens nuisance annoyance offensive quiet enjoyment",
    "[Describe the activity: noise, speakers, horns, sirens, odors, etc.] on [date(s) and time(s)] interfered with neighbors' quiet enjoyment.",
    "Stop the activity. No speakers, horns, sirens or other sound devices (other than security devices used only for security) may be located or used on a lot.")
add("D-5.6", "5.6", "Parking: front yard, street, sidewalk or inoperable vehicle", VEH, 7,
    "parking parked front yard lawn street overnight sidewalk inoperable vehicle driveway guest 24 hours",
    "A vehicle ([description / plate]) was observed [parked in the front yard off the driveway / parked on the street / parked over the sidewalk / inoperable outside a garage] on [date(s)].",
    "Move the vehicle. Vehicles may be parked in the front yard only on the driveway; street parking is prohibited except for guests (up to 24 hours, adjacent to the home) "
    "and oversized vehicles in daylight only; no vehicle may park over any part of a sidewalk; inoperable vehicles must be inside an enclosed garage.")
add("D-5.7a", "5.7", "Prohibited, oversized or commercial vehicle; vehicles on Common Area", VEH, 7,
    "commercial vehicle truck rv recreational vehicle camper trailer boat golf cart atv common area mobile home oversized heavy",
    "A [commercial vehicle / vehicle over 14,500 lbs / RV / trailer / camper / boat trailer / golf cart / ATV] ([description / plate]) was observed [on the Common Area / on the lot visible from neighboring property] on [date(s)].",
    "Remove the vehicle from the property or store it where it cannot be seen from neighboring property (rear or side yard or garage). Such vehicles may never be kept on the Common Area, "
    "and a guest's vehicle may stay no more than seven days in any six months.", start="(A)", end="(B)")
add("D-5.7b", "5.7", "Vehicle repair or unsightly / inoperable vehicle", VEH, 7, "vehicle repair inoperable unsightly missing parts broken down maintenance driveway",
    "A vehicle ([description / plate]) at [location] is [being repaired outside a garage / inoperable / missing parts or in poor repair].",
    "Move the vehicle into an enclosed garage or remove it. Vehicle repair (other than emergency repair) must be done in an enclosed garage, and inoperable or unsightly vehicles may not be kept on a lot.",
    start="(B)", end="(C)")
add("D-5.7c", "5.7", "Commercial or heavy vehicle visible from neighboring property", VEH, 7, "commercial vehicle lettering ladder racks tools trade name work truck visible",
    "A commercial vehicle ([description / plate]) displaying [business name / ladders / tools / trade goods] was parked where it is visible from neighboring property on [date(s)].",
    "Do not park the vehicle where it can be seen from neighboring property. Vehicles over 14,500 lbs (Class 5 or above) and commercial vehicles (those showing a business name or carrying "
    "trade goods, tools or ladders) must be out of sight.", start="(C)", end="(D)")
add("D-5.9", "5.9", "Machinery or equipment on or beside a lot", TRASH, 15, "machinery equipment tools generator compressor stored operated lot",
    "[Machinery / equipment] ([describe]) was observed [placed / operated] at [location].",
    "Remove the machinery or equipment. Only equipment usual and customary for the use and maintenance of the home may be placed or operated on or beside a lot.")
add("D-5.11", "5.11", "Window treatments / reflective materials", ALT, 30, "window treatments blinds curtains foil reflective mirror tint street facing permanent",
    "Windows facing the street at [location] have [no permanent window treatments (more than 30 days after occupancy) / foil or other reflective material].",
    "Install permanent, suitable window treatments on all street-facing windows within 30 days of occupancy, and remove any aluminum foil, mirrors or reflective material. "
    "Professional window tinting needs prior written approval.")
add("D-5.12", "5.12", "Heating, air conditioning or solar equipment without approval", ALT, 15, "hvac air conditioner heating cooling solar panels equipment approval",
    "[HVAC / solar] equipment ([describe]) was installed at [location] with no Architectural Committee approval on file.",
    "Submit an application to the Architectural Committee for the equipment, or remove it. HVAC units and solar panels need prior written approval of the equipment and its location.")
add("D-5.13", "5.13", "Garage: not kept clean, converted or left open", MAINT, 15, "garage door open clutter living space converted storage neat clean",
    "The garage at [location] [is not neat and clean / has been converted to living or recreational space / is being used for storage beyond normal household supplies / was left open when not needed].",
    "Restore the garage to its approved use: keep it neat and clean, use it only for parking vehicles and storing normal household supplies, do not convert it to living quarters or recreation "
    "space without Architectural Committee approval, and keep the garage door closed except as needed for coming and going and ventilation while working inside.")
add("D-5.14", "5.14", "Landscaping and irrigation not installed", LAND, 120, "landscaping irrigation front yard bare new owner installed weeds 120 days",
    "Landscaping and irrigation have not been installed in [the front yard / the yard] at [location] more than 120 days after the current Owner acquired the lot.",
    "Install landscaping and irrigation in line with the Architectural Committee Rules and approved plans, and keep the area weed-free until then. The Association may install it and charge the cost to the Owner.",
    start="(A)", end="(B)")
add("D-5.16-short", "5.16", "Lease for less than 30 days (short-term rental)", LEASE, 7, "short term rental airbnb vrbo vacation rental less than 30 days lease nightly weekly",
    "The home at [address] was [advertised / occupied] under a rental of less than 30 days ([describe: listing, dates, evidence]).",
    "Stop renting the home for less than 30 days. No lot may be leased for a period shorter than thirty (30) days.")
add("D-5.16-docs", "5.16", "Tenant violations / documents not given to tenant", LEASE, 15, "tenant lease rules documents governing documents owner responsible tenant violation",
    "The tenant at [address] [violated the Association's documents ([describe]) / was not given copies of the governing documents].",
    "Give your tenant (and any management company) copies of the Declaration and the Association's rules and make sure they are followed. The Owner is responsible for violations by "
    "tenants and occupants.")
add("D-5.16.2", "5.16.2", "Tenant information not given to the Association", LEASE, 15, "tenant information lease notice name address phone email rented leased",
    "The Association has no written notice of the tenant(s) at [address], which is [rented / leased].",
    "Send the Association written notice of each tenant: full name and mailing address, plus phone number, fax and email address where available.")
add("D-5.16.1", "5.16.1", "Property manager information not given to the Association", LEASE, 15, "property manager leasing agent management company rental notice",
    "The owner of [address] uses a [property manager / leasing agent / management company] but has not given the Association the required written notice.",
    "Send the Association written notice of the manager: the manager's full name and mailing address, phone, fax and email, a contact person if it is a company, and an emergency contact.")
add("D-5.17", "5.17", "Animals not permitted or kept improperly", PETS, 7, "animals pets dog cat livestock chickens poultry leash waste nuisance bark number commercial breeding",
    "[Describe: a type of animal that is not permitted / an unreasonable number of animals / commercial breeding / a pet off its leash or outside a fenced yard / pet waste not removed / an animal structure visible from neighboring property / a nuisance animal] at [location] on [date].",
    "Comply with Section 5.17: keep only dogs, cats or other common household pets approved by the Board, not for commercial purposes and not in unreasonable numbers; keep pets on a leash or in a "
    "fenced yard; pick up and dispose of all pet waste; keep animal housing out of view; and stop any nuisance.")
add("D-5.19", "5.19", "Irrigation well", LAND, 15, "irrigation well pump well water sprinkler",
    "An individual irrigation well [has been installed / is in use] at [location].",
    "Stop using the well and remove it. No owner may use or install an individual irrigation well.")
add("D-5.20", "5.20", "Refuse: not in closed containers, not hidden, or accumulating", TRASH, 7, "refuse trash garbage containers lids open smell sanitary accumulate waste animal waste",
    "[Refuse / pet waste] was [accumulating on the lot / in containers without operable lids or not clean and sanitary / visible from neighboring property] at [location] on [date].",
    "Remove accumulated refuse regularly, keep it in closed containers with operable lids, keep containers clean and free of odors, and keep them out of view of neighboring property except for the shortest time needed for collection.")
add("D-5.21", "5.21", "Antenna or satellite dish not permitted", ALT, 15, "antenna satellite dish mast receiver tv radio",
    "An [antenna / satellite dish / mast] was observed at [location] [with no Architectural Committee approval / visible from neighboring property].",
    "Submit an Architectural Committee application, or relocate or remove the equipment. Receivers must be placed where they are not visible from neighboring property if that does not unreasonably "
    "delay, add cost to, or prevent an acceptable signal, and be screened.", start="(C)", end="(D)")
add("D-5.23", "5.23", "Conditions that breed disease or insects (e.g. unclean pool)", MAINT, 7, "pool green stagnant mosquitoes insects disease standing water unclean inoperable",
    "A condition at [location] ([e.g. a pool that is not operable and clean / standing water / other]) is likely to breed insects or disease.",
    "Correct the condition at once: keep swimming pools operable and clean, and remove standing water or anything else that harbors infectious plant diseases or noxious insects.")
add("D-5.24", "5.24", "Excavation, grading or construction without approval", ALT, 15, "excavation grading construction digging fill new construction approval",
    "[Excavation / grading / construction] ([describe]) is under way at [location] with no written approval on file.",
    "Stop the work and submit it for approval. No excavation, grading or construction may be done on a lot by an Owner without prior written approval.")
add("D-5.25b", "5.25", "Improvement or exterior change without Architectural Committee approval", ALT, 15,
    "improvement alteration addition exterior change paint color unapproved architectural committee approval construction installed",
    "An exterior [improvement / alteration / color change] ([describe]) was made at [location] without written Architectural Committee approval.",
    "Submit an application to the Architectural Committee for the work, or restore the property to its previous condition. No improvement or exterior change, including color scheme, may be made without prior written approval.",
    start="(B)", end="(D)")
add("D-5.25a", "5.25", "Grading or drainage altered", WATER, 15, "grading drainage alter regrade stormwater flow swale elevation",
    "The grading or drainage at [location] has been altered ([describe]).",
    "Restore the original grading and drainage and make sure storm water flows as it did before. Alterations must comply with the approved grading and drainage plan and need Architectural Committee approval.",
    start="(A)", end="(B)")
add("D-5.25e", "5.25", "Fence not meeting the Declaration's guidelines", ALT, 15, "fence pvc wrought iron aluminum height color black beige encroach front wall",
    "A fence at [location] [was installed without approval / exceeds the height limit / is made of a non-permitted material / is the wrong color / extends past the front or rear wall / encroaches on Common Area or another lot].",
    "Correct or remove the fence, or obtain written approval. Fences need Architectural Committee approval, may be no higher than six feet (five feet beyond the rear wall of water-feature or conservation lots), "
    "must be PVC (beige), wrought iron or aluminum rail (black), and may not encroach.", start="(E)", end="(F)")
add("D-5.26", "5.26", "Clothesline or outdoor drying visible", TRASH, 7, "clothesline clothes drying laundry airing visible",
    "An outdoor clothesline or clothes-drying facility is visible from neighboring property at [location].",
    "Remove the clothesline or drying facility, or place it where it cannot be seen from neighboring property.")
add("D-5.27", "5.27", "Tree or shrub overhanging sidewalk or street", LAND, 15, "tree shrub overhang encroach sidewalk street trim branches 8 feet",
    "A [tree / shrub / planting] at [location] overhangs or encroaches on the [sidewalk / street / pedestrian way] below a height of eight feet.",
    "Trim the plant back so nothing overhangs or encroaches on a sidewalk, street or pedestrian way from the ground to a height of eight feet.")
add("D-5.28", "5.28", "Drainage obstructed or grade changed", WATER, 15, "drainage obstructed blocked water flow grade elevation surface water management swale",
    "[A structure / landscaping / fence / fill] at [location] obstructs the surface water drainage or changes its direction, or the grade of the lot has been changed.",
    "Remove the obstruction and restore the original grade so water drains as shown on the approved drainage plans.")
add("D-5.29", "5.29", "Basketball goal not in accordance with the rules", REC, 15, "basketball hoop goal backboard rule",
    "A basketball [goal / hoop / backboard] at [location] does not comply with the Architectural Committee Rules.",
    "Remove the basketball goal or bring it into line with the Architectural Committee Rules and obtain any approval required.")
add("D-5.30", "5.30", "Playground equipment visible without approval", REC, 15, "playground swing set jungle gym play equipment visible",
    "[Swing set / jungle gym / playground equipment] is visible from neighboring property at [location] with no written approval.",
    "Remove the equipment or obtain written approval from the Architectural Committee before it is installed where it can be seen from neighboring property.")
add("D-5.31", "5.31", "Spotlights or floodlights shining on other property", LIGHT, 7, "spotlight floodlight bright light shining neighbor glare",
    "[Spotlights / floodlights / high-intensity lighting] at [location] direct or reflect light onto other property.",
    "Remove or re-aim the lights so no light is directed or reflected onto any other property, or obtain approval.")
add("D-5.32", "5.32", "Flag not meeting the Declaration's rules", SIGN, 7, "flag flagpole pole height size lighting condition",
    "A [flag / flagpole] at [location] [is not a permitted flag / is too large or too tall / is in poor condition / obstructs sight lines or is on an easement].",
    "Bring the flag display into compliance with Section 5.32: the U.S. and Florida flags (plus one military or POW-MIA flag) are allowed; a detached pole may be no higher than 20 feet, an attached pole "
    "no longer than 10 feet; flags no larger than 4½ by 6 feet; displayed per the Flag Code, kept in good condition, and not on an easement or blocking sight lines.")
add("D-5.33", "5.33", "Yard sale outside the allowed limits", SIGN, 7, "yard sale garage sale days hours signs permit",
    "A yard sale was held at [location] on [date(s)] [more than two days a year / outside 6:00 a.m.-5:00 p.m. / with signs posted around the community / without required permits].",
    "Hold no more than two yard-sale days a year, between 6:00 a.m. and 5:00 p.m., with a sign only on your own lot on the day of the sale, on any dates the Association sets, and with the required permits.")
add("D-5.34", "5.34", "Holiday decorations outside the allowed period or too bright / loud", LIGHT, 7, "holiday decorations lights christmas inflatables display sound november january",
    "Holiday decorations ([describe]) are displayed at [location] [outside the allowed period / with excessive light or sound / causing excessive spectator traffic].",
    "Remove the decorations. They may be displayed November 1 to January 31 and, otherwise, from one week before to one week after a nationally recognized holiday, and must be of reasonable size and "
    "not disturb neighbors with light, sound or traffic.")
add("D-5.35", "5.35", "Screen enclosure not meeting the guidelines", ALT, 15, "screen enclosure lanai cage aluminum bronze black mesh kick plate opaque front entry",
    "A screen enclosure at [location] [was installed without approval / is not an aluminum frame in bronze or black / has a non-standard or opaque screen / has a kick plate over 24 inches / extends beyond the covered front entry].",
    "Correct or remove the enclosure, or obtain written approval. Enclosures need Architectural Committee approval, must be aluminum in bronze or black with a standard dark screen mesh, "
    "kick plates no taller than 24 inches, no opaque screening, and none at a front entry beyond the covered entry.")
add("D-5.36", "5.36", "Firearms or weapons", OTHER, 3, "firearm gun weapon discharge bb gun pellet knife sword crossbow",
    "[Describe: use or discharge of a firearm or weapon / carrying a weapon in the Common Areas without a permit] on [date] at [location]. (Consider notifying law enforcement.)",
    "Stop immediately. The use or discharge of firearms or other weapons within the Property is prohibited, and weapons may not be carried in the Common Areas without a permit.")
add("D-7.3", "7.3", "Party wall: sprinklers, trees or attachments", MAINT, 15, "party wall boundary wall sprinkler water tree within 6 feet vine attached",
    "At [location], [sprinklers / water spray reaches the boundary wall / a tree is within six feet of the party wall / something is attached to the party wall (e.g. a climbing vine)].",
    "Correct it: do not let sprinklers or other water sources wet a boundary wall, keep trees at least six feet from the wall, and attach nothing (including vines) to a party wall. "
    "Any resulting damage is the responsibility of the owner who caused or allowed it.")
add("D-7.4", "7.3", "Party wall damaged and not repaired", MAINT, 15, "party wall damaged repair broken crack boundary wall",
    "The party wall at [location] is damaged ([describe]) and has not been repaired.",
    "Repair the party wall to its prior condition. Damage caused by an owner or the owner's guests, tenants or family must be repaired immediately at that owner's cost; otherwise the repair "
    "responsibility is set by Article VII.", start="Section 7.3", end="No Owner shall violate any of the following")
add("D-8.2", "8.2", "Drainage easement blocked or diverted", WATER, 15, "drainage easement blocked diverted channel water flow swale",
    "Drainage flow in the drainage easement at [location] has been [obstructed / diverted] ([describe]).",
    "Remove the obstruction and restore the drainage easement. Drainage flow must not be obstructed or diverted from drainage easements.",
    start="Section 8.2", end="Land Owner hereby reserves")
add("D-9.1", "9.1", "Lot or home not maintained in good repair", MAINT, 15, "maintenance repair yard landscaping neat disrepair condition absence upkeep appearance",
    "The [home / yard / landscaping] at [location] is not maintained in good repair ([describe]).",
    "Repair and maintain the home and yard so they are in good repair, neatly and attractively kept and in harmony with other homes. The Association may do the work and charge the Owner.")
add("D-9.1s", "9.1", "Sidewalk needs pressure washing", MAINT, 30, "sidewalk pressure wash mold mildew algae dirty",
    "The sidewalk at [location] has an unreasonable accumulation of mold or mildew.",
    "Pressure-wash the sidewalk to remove mold and mildew. The Owner is responsible for pressure-washing the sidewalk, as needed, to keep it free of unreasonable mold and mildew.",
    start="If any sidewalk is partially", end="+as needed).")
add("D-9.1r", "9.1", "Right-of-way landscaping between the lot and the curb not maintained", LAND, 15, "right of way strip curb landscaping grass between sidewalk street verge",
    "The landscaping in the right-of-way between the lot and the curb at [location] is not maintained ([describe]).",
    "Maintain the landscaping in the right-of-way between your lot and the curb in good condition and repair.",
    start="Except for areas owned by the Association", end="+adjacent street.")
add("D-9.3", "9.3", "Damage to the Common Area", OTHER, 15, "common area damage destroyed vandalism broken repair",
    "[Describe the damage] to the Common Area at [location] was [caused by / allowed by] the Owner (or the Owner's tenant or guest) on [date].",
    "Pay the Association's cost to repair the damage on demand, and stop any interference with the Association's work on the Common Area.")
add("D-10.5", "10.5", "Required owner insurance not carried", OTHER, 30, "insurance property liability coverage owner proof",
    "The Association has no evidence that the Owner of [address] carries the insurance the Declaration requires.",
    "Provide proof of property insurance on the lot, its improvements and personal property, and personal liability coverage for the lot.")
add("D-14.4", "14.4", "Drainage swale filled, dug, fenced or damaged", WATER, 15, "swale drainage fill excavation fence obstruct damaged mowing erosion water management district",
    "The drainage swale on the lot at [location] has been [filled / excavated / fenced / obstructed / damaged] ([describe]).",
    "Restore the swale to its former condition as soon as possible. Filling, excavating, building fences or otherwise obstructing the swale is prohibited; the Owner must maintain it (mowing, erosion repair).",
    start="The Declarant may have constructed", end=None)
add("D-14.6.3", "14.6", "Pond littoral area vegetation altered", WATER, 7, "pond littoral native plants vegetation cut mow pulled planted grass carp lake shoreline county",
    "Vegetation in the littoral area of the pond behind [address] has been [cut / mowed / pulled / planted / treated] ([describe]).",
    "Stop altering the littoral vegetation. No Owner may alter vegetation growing in a pond's littoral area (cutting, mowing, pulling, planting, or introducing grass carp) without written "
    "authorization from Sarasota County's Environmental Protection Division.", start="Section 14.6.3", end=None)


HEADING_OVERRIDE = {"D-14.6.3": "County Provisions (littoral areas)", "D-7.4": "Damage by One Owner"}


def parse(txt):
    lines = Path(txt).read_text(encoding="utf8").split("\n")
    start = next(i for i, l in enumerate(lines) if re.match(r"^\s*USE RESTRICTIONS\s*$", l))   # Article V heading (not the table of contents)
    foot = re.compile(r"9/27/|^\s*\d{1,3}\s*$|^\s*ARTICLE [IVX]+\s*$")
    head = re.compile(r"^\s*Section\s+(\d+\.\d+(?:\.\d+)?)\s+([A-Z][^.]{2,90}?)\.\s*(.*)$")
    secs, cur = {}, None
    for l in lines[start:]:
        if re.match(r"^\s*ARTICLE XV\s*$", l):          # the Declaration's last article before the signature pages / Bylaws
            break
        if foot.search(l) and len(l.strip()) < 40:
            continue
        m = head.match(l)
        if m and m.group(1).split(".")[0] in ARTICLES:
            cur = m.group(1)
            secs[cur] = {"title": m.group(2).strip(), "lines": [f"Section {cur} {m.group(2).strip()}. {m.group(3)}"]}
        elif cur:
            secs[cur]["lines"].append(l.strip())
        elif (m2 := re.match(r"^\s*Section\s+(14\.6\.3)\s+(.*)$", l)):
            pass
    return repair({k: clean(" ".join(v["lines"])) for k, v in secs.items()})


def repair(secs):
    """The scan's text layer has pages 26-28 out of order, so the tail of 5.14, 5.16 and 5.17 landed under the wrong headings. Put them back."""
    a14, a16, a17 = secs["5.14"], secs["5.16"], secs["5.17"]
    pets_at = a16.index("common household pets")                       # 5.17's second half, mis-filed under 5.16
    pets_tail, a16 = a16[pets_at:], a16[:pets_at].rstrip()
    leasing_tail_at = a14.index("provisions of this Section 5.16 shall not apply")   # 5.16's last sentence, mis-filed under 5.14
    leasing_tail, a14 = a14[leasing_tail_at:], a14[:leasing_tail_at].rstrip()
    drop_at = a17.index("collection of such amounts")                   # 5.14(B)-(D), mis-filed under 5.17
    a14_rest, a17 = a17[drop_at:], a17[:drop_at].rstrip()
    secs["5.14"] = a14 + " " + a14_rest
    secs["5.16"] = a16 + " " + leasing_tail
    secs["5.17"] = a17 + " " + pets_tail
    assert secs["5.16"].endswith("marketing purposes."), secs["5.16"][-80:]
    assert secs["5.17"].endswith("deposited on any portion of the Property by such Owner’s pet."), secs["5.17"][-80:]
    assert secs["5.14"].rstrip().endswith("has not sold.") or "Section 5.15" not in secs["5.14"], secs["5.14"][-80:]
    secs["5.36"] = secs["5.36"][:secs["5.36"].index("regardless of size.") + len("regardless of size.")]   # drop the next article's heading
    return secs


def clean(t):
    t = re.sub(r"\s+", " ", t).strip()
    for a, b in FIX:
        t = t.replace(a, b)
    return t


def cut(text, start, end):
    """The part of a section from `start` up to `end`. An `end` beginning with '+' is kept in the quote (the quote ends after it)."""
    s = text
    if start:
        i = s.find(start)
        if i < 0:
            raise ValueError(f"marker {start!r} not found")
        s = s[i:]
    if end:
        keep = end.startswith("+")
        end = end.lstrip("+")
        j = s.find(end, len(start or "") + 1)
        if j < 0:
            raise ValueError(f"marker {end!r} not found")
        s = s[:j + (len(end) if keep else 0)]
    return s.strip()


def main(txt):
    secs = parse(txt)
    items = []
    for d in D:
        sec = d["section"]
        text = secs[sec]
        if d["code"] == "D-14.6.3":
            text = secs["14.6"]
        rule = cut(text, d["start"], d["end"])
        # the Declaration's own heading for the section, for the "COVENANTS:" line on letters (e.g. "Signs")
        heading = HEADING_OVERRIDE.get(d["code"]) or re.match(r"Section [\d.]+ (.+?)\.\s", secs[sec]).group(1)
        items.append({k: v for k, v in d.items() if k not in ("start", "end")} | {"standards_title": heading, "rule": rule, "source": "Declaration"})
    assert len({i["code"] for i in items}) == len(items), "duplicate codes"
    out = Path(__file__).resolve().parent.parent / "violations" / "data" / "declaration_catalog.json"
    out.write_text(json.dumps({"source": SOURCE, "items": items}, indent=1, ensure_ascii=False), encoding="utf8")
    print(f"wrote {len(items)} items to {out}")


if __name__ == "__main__":
    main(sys.argv[1])
