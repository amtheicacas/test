"""Build violations/data/standards_catalog.json from the Community Standards PDF.

    pdftotext -layout Community_Standards.pdf standards.txt
    python tools/build_standards_catalog.py standards.txt

Rule text is copied verbatim from the PDF (so the app always shows the Association's actual wording).
The per-section drafts below (category, search keywords, 'observed' and 'cure' text, cure days) are the part
people edit. After the standards are revised: re-extract the text, adjust the drafts, re-run, then
`python manage.py load_standards`. Anything in [square brackets] must be filled in by whoever issues the notice;
the app refuses to send a notice that still contains one.
"""
import json
import re
import sys
from pathlib import Path

# (code, section, title, category, cure_days, keywords, observed, cure)
D = []


def add(code, section, title, category, days, keywords, observed, cure):
    D.append(dict(code=code, section=section, title=title, category=category, cure_days=days, keywords=keywords,
                  observed=observed, cure=cure))


ALT, VEH, TRASH, LIGHT, SIGN, PETS, REC, LAND, MAINT, MISC = (
    "Alterations & approvals", "Vehicles & parking", "Trash, storage & clutter", "Lighting & decorations",
    "Signs, flags & events", "Pets & nuisance", "Recreation & play", "Landscaping & lawn",
    "Maintenance & appearance", "Other")

# ---- Alterations & approvals ------------------------------------------------------------------
add("3.01", "3.01", "Exterior alteration without approval", ALT, 15,
    "unapproved alteration modification addition change exterior no approval acc application permit renovation",
    "An exterior alteration or modification ([describe the work and where]) was observed at the property. "
    "No approved Architectural Control Committee (ACC) application is on file.",
    "Submit a completed Application to the Architectural Control Committee for this work, or restore the property to "
    "its previous condition, by the deadline. Do not continue the work until written Board approval is received.")
add("3.05", "3.05", "Awning", ALT, 15, "awning canopy lanai cover shade",
    "An awning was observed at [location] that has no ACC approval on file or is not on the rear of the home over the "
    "lanai in a neutral color matching the exterior.",
    "Remove the awning, or obtain prior Board approval. Approved awnings must be on the rear of the home over the lanai, "
    "neutral in color and matching the exterior color of the home.")
add("3.15", "3.15", "Exterior color / repainting", ALT, 45, "paint color repaint peeling faded stained exterior finish stone",
    "The exterior of the home ([describe: unapproved color / peeling or faded paint / stained]) does not meet the "
    "approved color scheme or is in need of repainting.",
    "Repaint the exterior in an approved community color after submitting an ACC application, or restore the previously "
    "approved color. Homes must be repainted within 45 days of notice.")
add("3.17", "3.17", "Fence, wall or screen enclosure", ALT, 15,
    "fence wall privacy chain link wood pvc screen enclosure screening wrought iron aluminum cage",
    "A [fence / wall / screen enclosure] was observed at [location] that was installed without ACC approval or is a "
    "prohibited type (wood, chain link, PVC or privacy fencing).",
    "Remove the fence/wall/enclosure, or replace it with an approved type after receiving written ACC approval. "
    "Only wrought iron or black aluminum fences 4' to 6' high are allowed; screen enclosures must be bronze with "
    "charcoal screening.")
add("3.18", "3.18", "Fountain or sculpture", ALT, 15, "fountain sculpture statue water feature",
    "A [fountain / sculpture] was observed at [location]. Fountains and sculptures are not permitted on the property.",
    "Remove the fountain or sculpture from the property.")
add("3.19", "3.19", "Front door / entryway", ALT, 15,
    "front door entryway entry porch alcove clutter shoes trellis arbor decorative items paint color",
    "The front entry ([describe: unapproved door/color / more than the allowed decorative items / clutter or stored "
    "personal items]) does not comply with the Community Standards.",
    "Correct the entryway: obtain ACC approval for door style/color changes, limit decorative items to two (only one "
    "wall-mounted, none over 3 feet unless wall mounted), and remove stored shoes, clutter and personal items.")
add("3.21", "3.21", "Garage door damaged or non-conforming", ALT, 30, "garage door damaged dent warped panel window carport",
    "The garage door has [describe: dents / warping / finish deterioration / cracked or broken panels / alignment "
    "problems / windows / non-standard design].",
    "Repair or replace the damaged garage door panels to the satisfaction of the Association (garage doors must be "
    "standard solid panel construction with no windows) within 30 days of notice.")
add("3.21-open", "3.21", "Garage door left open", MAINT, 7, "garage door open left open closed",
    "The garage door was observed open on [date(s)/time(s)] when no access or activity required it to be open.",
    "Keep the garage door closed except when access or an activity requires it to be open.")
add("3.26", "3.26", "Glass block", ALT, 15, "glass block window",
    "Glass block was observed on the [home / addition]. Glass block is not permitted.",
    "Remove the glass block and replace it with an approved window treatment after ACC approval.")
add("3.31", "3.31", "Hurricane shutters left closed", ALT, 7, "hurricane shutters storm panels accordion roll-up closed",
    "Hurricane shutters/panels were observed closed with no hurricane watch or warning in effect.",
    "Open or remove the hurricane shutters. Shutters may be closed only up to 72 hours before an expected hurricane and "
    "must be opened or removed within one week after the watch or warning ends.")
add("3.38", "3.38", "Porch, deck, patio or screen enclosure", ALT, 15,
    "porch deck patio lanai screen enclosure roof mold mildew screened front porch",
    "A [porch / deck / patio / screen enclosure] at [location] ([describe: no ACC approval / non-compliant roof or "
    "materials / not finished to the ground / mold or mildew]) does not comply with the Community Standards.",
    "Obtain ACC approval for the structure or correct it to comply (consistent appearance, bronze/black aluminum with "
    "charcoal screen, compliant roof, finished to the ground and free of mold, mildew and weeds). Screened front "
    "porches are not permitted.")
add("3.45", "3.45", "Roof repair or change", ALT, 45, "roof roofing shingles tiles damage leak tarp repair",
    "The roof ([describe: damaged or missing shingles/tiles / tarp / unapproved roofing change]) requires repair or approval.",
    "Complete routine roof repairs within 45 days of notice, or submit an ACC application for any roof change. "
    "If the damage resulted from a catastrophic weather event, keep management informed about insurance, warranty and "
    "contractor status so a reasonable timeline can be agreed.")
add("3.46", "3.46", "Satellite dish / antenna", ALT, 15, "satellite dish antenna mast tower aerial",
    "A [satellite dish / antenna / mast] was observed at [location] without ACC approval or visible from the street/neighbors.",
    "Remove the equipment or obtain prior ACC approval for the location and screening. Dishes over 39.37 inches are prohibited.")
add("3.49", "3.49", "Solar equipment", ALT, 15, "solar panels heating equipment roof front",
    "Solar equipment was observed [installed without ACC approval / on the front of the home or front portion of the property].",
    "Remove the equipment or obtain ACC approval. Solar equipment may not be installed on the front of the home or the front portion of the property.")
add("3.52", "3.52", "Pool, spa or pool project", ALT, 30,
    "pool spa hot tub whirlpool pool cage screen torn debris sidewalk damage cleanliness above ground equipment",
    "[Describe: unapproved pool/spa/cage / torn or damaged pool cage screen / pool equipment not screened / debris on "
    "sidewalk or street / damaged sidewalk / lawn and beds not restored / chlorinated water discharged].",
    "Correct the condition: obtain ACC approval for pool work; repair or replace a damaged pool cage within 90 days; "
    "screen pool equipment with landscaping; keep sidewalks and streets clear of debris; repair community property damage "
    "and restore lawn, beds and irrigation within 30 days of completion.")
add("3.67", "3.67", "Window treatments", ALT, 15,
    "window treatment blinds drapes sheet foil newspaper tint reflective security bars signs neon rope lights",
    "Window treatments at [location] ([describe: temporary covering such as sheets/foil/newspaper / signs or decorative "
    "items in window / reflective tint / security bars / non-neutral color]) do not comply with the Community Standards.",
    "Remove the non-compliant items and use only blinds, neutral drapes or shutters. Obtain written approval before "
    "installing security bars, reflective tinting or exterior shutters/awnings.")
add("3.68", "3.68", "Window or wall air conditioner / fan", ALT, 15, "window air conditioner ac unit wall fan",
    "A [window / wall] air conditioning unit or window fan was observed at [location]. These are prohibited.",
    "Remove the window or wall unit/fan.")
add("3.28", "3.28", "Air conditioning equipment unscreened", ALT, 15, "air conditioner ac condenser equipment screening shrubs noise",
    "Outdoor air conditioning equipment at [location] is not screened from view of the street and neighboring homes.",
    "Screen the equipment with shrubs and/or approved fencing (landscaping plan or fence application to the ACC as required).")
add("3.41", "3.41", "Rain barrel", ALT, 15, "rain barrel cistern water collection",
    "A rain barrel was observed at [location] [without ACC approval / visible from the street / not neutral or earth-toned].",
    "Obtain ACC approval and store the barrel behind the home out of view from any street in a neutral or earth-tone color, or remove it.")
add("3.63", "3.63", "Water softener unscreened", ALT, 15, "water softener conditioner screening",
    "A water softener at [location] is visible from the street/neighbors and is not screened.",
    "Screen the water softener from view with landscaping or approved fencing.")
add("3.69", "3.69", "Yard ornament", ALT, 1, "yard ornament decoration statue gnome figurine decor garden art",
    "[Number] yard ornament(s) ([describe]) were observed at [location] without ACC approval, or exceeding two ornaments / 36 inches in height.",
    "Remove the unauthorized yard ornaments within 24 hours of notice. Up to two ornaments under 36 inches tall are allowed only with prior ACC approval and only in a landscape bed.")
add("3.44", "3.44", "Damage not repaired", MAINT, 30, "damage casualty fire storm destroyed repair rebuild",
    "The [home / improvement] at [location] has [describe damage] and repair or rebuilding has not been commenced.",
    "Commence repair or rebuilding of the damaged home or improvement in accordance with the Declaration, "
    "and provide the Association with a schedule. Notify management of the insurance status.")

# ---- Vehicles & parking -----------------------------------------------------------------------
add("3.04", "3.04", "ATV / dirt bike", VEH, 7, "atv dirt bike four wheeler motorcycle off road golf cart scooter minibike",
    "An [ATV / dirt bike] was observed [being driven / parked or stored outside the garage] on [date].",
    "Store ATVs and dirt bikes in the garage and transport them out of the neighborhood on a trailer. They may not be driven anywhere in Heron Landing.")
add("3.09", "3.09", "Commercial vehicle on driveway", VEH, 7, "commercial vehicle work truck van lettering ladder rack driveway",
    "A commercial vehicle ([description / plate]) was observed parked on the driveway on [date(s)].",
    "Park commercial vehicles (work vehicles, vehicles with writing on the sides, or equipment racks/holders) inside the garage at all times.")
add("3.40", "3.40", "Prohibited vehicle (RV, boat, trailer, camper, etc.)", VEH, 7,
    "recreational vehicle rv camper boat trailer limousine for sale sign domicile golf cart prohibited vehicle",
    "A [RV / camper / boat / trailer / limousine / vehicle displaying a for-sale or commercial sign] ([description / plate]) was observed parked outside a garage on [date(s)].",
    "Store the vehicle inside the garage, or remove it from Heron Landing. Vehicles bearing for-sale signs or commercial advertising may not be parked in public view, and no vehicle may be used as a residence.")
add("3.57", "3.57", "Trailer in public view", VEH, 7, "trailer flat bed cargo horse motorcycle boat rv public view",
    "A trailer ([type]) was observed in public view at [location] on [date(s)].",
    "Store the trailer in the garage and keep the garage door closed.")
add("3.64", "3.64", "Watercraft not stored in garage", VEH, 7, "boat jet ski canoe kayak watercraft paddleboard",
    "[Boat / jet ski / canoe / kayak] was observed stored outside the garage at [location].",
    "Store boats, jet skis, canoes, kayaks and other water equipment only inside the garage.")
add("3.43", "3.43", "Vehicle repair / inoperable vehicle", VEH, 7, "vehicle repair inoperable blocks tarp cover jack engine maintenance driveway",
    "[Describe: vehicle that cannot operate on its own power for more than 12 hours / vehicle repair or maintenance in the driveway / vehicle on blocks / tarp-covered vehicle] at [location].",
    "Move the vehicle into the garage. Vehicles that cannot operate on their own power may not remain more than 12 hours except in a garage; repairs (other than emergency repairs) must be done in a garage, and vehicles may not be stored on blocks or covered with tarps in public view.")
add("3.60-street", "3.60", "Vehicle parked on street overnight", VEH, 7, "overnight street parking parked in street curb",
    "A vehicle ([description / plate]) was observed parked on the street overnight on [date(s)].",
    "Park vehicles in the garage or driveway. Parking on the street overnight is not allowed.")
add("3.60-sidewalk", "3.60", "Vehicle blocking sidewalk", VEH, 7, "sidewalk blocking driveway parked vehicle blocking",
    "A vehicle ([description / plate]) was observed blocking the sidewalk on [date(s)].",
    "Park vehicles on the driveway so that the sidewalk remains clear. Vehicles may not block the sidewalk.")
add("3.60-unlicensed", "3.60", "Unlicensed / damaged vehicle", VEH, 7, "unlicensed tag expired damaged vehicle rust dented noise smoke",
    "A vehicle ([description / plate]) with [no valid tag displayed / significant damage, rust or disrepair] was observed outside the garage.",
    "Properly license the vehicle with tags clearly displayed, or store it inside the garage. Damaged vehicles must be stored in the garage.")
add("3.60-surface", "3.60", "Vehicle on grass, common area or guest parking", VEH, 7, "parked on lawn grass common area guest parking",
    "A vehicle ([description / plate]) was observed parked on [the lawn / common area paved surface / a guest parking space] on [date(s)].",
    "Park only in the garage or on the driveway. Owners may not park on lawns, on the paved Common Area or in guest parking spaces.")

# ---- Trash, storage & clutter -----------------------------------------------------------------
add("3.14", "3.14", "Equipment stored outside", TRASH, 15, "equipment stored outside lawn care car care tools ladder mower work equipment",
    "[Lawn care / car care / work] equipment ([describe]) was observed stored outside the residence at [location].",
    "Store all equipment inside the residence or garage, out of view.")
add("3.22", "3.22", "Garbage cans visible / not shielded", TRASH, 15,
    "garbage can trash can bin bins waste container visible shielded screened side of house smell odor rodents",
    "Garbage/trash cans were observed at [location] visible from the street or neighboring homes and not shielded from view.",
    "Store garbage cans inside the garage or on the side of the home fully shielded from view, with lids secured. "
    "To use an outside location, submit a plan to the ACC to shield the cans (at least 80% screened with tall/mature plants at first, fully screened within 12 months).")
add("3.22-timing", "3.22", "Garbage cans out too early / not returned", TRASH, 7, "garbage can trash bins curb pickup day early late returned timing",
    "Garbage containers were observed at the curb on [date/time], outside the permitted window (not before 5:00 p.m. the day before pickup; returned by 10:00 p.m. on pickup day).",
    "Place containers at the curb no earlier than 5:00 p.m. the day before pickup and return them to their concealed location no later than 10:00 p.m. on pickup day.")
add("3.22-debris", "3.22", "Trash, litter or debris in plain view", TRASH, 7, "trash litter debris junk bulk items yard waste plain view windblown",
    "Trash, litter or debris ([describe]) was observed in plain view at [location].",
    "Remove all trash, litter and debris from view and keep the property free of windblown debris. No trash of any kind may be stored in plain view.")
add("3.23", "3.23", "Garden hose / reel", TRASH, 15, "garden hose reel hanger retracted coiled",
    "Garden hose(s) were observed [left unrolled on the ground / on a non-neutral colored reel or hanger / visible from the street] at [location].",
    "Fully retract and neatly hang hoses when not in use. Hangers, reels and hoses must be placed on the side of the home, concealed from the street, and in neutral colors (dark green, brown, black, earth tones).")
add("3.27", "3.27", "Grill", TRASH, 7, "grill bbq barbecue cooker outdoor cooking",
    "A grill was observed [uncovered / stored in front of the home or visible from the street / in use on common area] at [location].",
    "Cover the grill and store it behind the home on the patio/lanai when not in use. No cooking is permitted on the Common Areas.")
add("3.33", "3.33", "Laundry / rugs visible", TRASH, 7, "laundry clothes towels rugs mops hanging drying visible",
    "Laundry, rugs or similar items were observed hung or exposed at [location], visible from outside the home or lot.",
    "Remove laundry, rugs, mops and similar articles from view.")
add("3.36", "3.36", "Patio / lanai furniture, indoor furniture or appliances", TRASH, 15,
    "patio furniture lanai front porch indoor furniture appliance couch refrigerator driveway",
    "[Indoor furniture / a full-size appliance / furniture on the driveway / non-neutral or unapproved front furniture] was observed at [location].",
    "Remove the item. Indoor furniture and household appliances may not be placed in front yards, patios, gardens or flowerbeds; no furniture is allowed on driveways; front-area furniture requires ACC approval and must be neutral outdoor furniture.")
add("3.53", "3.53", "Fuel / propane tank / hazardous substances", TRASH, 15, "propane tank fuel gasoline flammable hazardous chemical gas bottle",
    "[Propane tank / fuel / flammable or hazardous substance] was observed at [location] [visible / stored outside normal household use].",
    "Install propane and bottled gas tanks underground or screen them from view with ACC-approved landscaping or materials, and remove any flammable, combustible or hazardous substances beyond normal household use.")
add("3.54", "3.54", "Temporary storage container (POD)", TRASH, 7, "pod storage container dumpster temporary container pod unit",
    "A temporary storage container/POD was observed at [location] since [date] [without notifying the Association / beyond 14 days / blocking the sidewalk or roadway].",
    "Remove the container. Only one container is allowed, for no more than 14 days, not blocking the sidewalk or roadway, and the Association must be notified of the arrival and pick-up dates.")
add("3.55", "3.55", "Temporary structure / tent", TRASH, 7, "temporary structure tent gazebo canopy pop up shed trailer building",
    "A temporary [structure / tent / gazebo / building] was observed at [location] on [date(s)].",
    "Remove the structure. Pop-up tents and gazebos may be used only temporarily for events and may not be stored in yards or used regularly.")

# ---- Lighting & decorations -------------------------------------------------------------------
add("3.16", "3.16", "Exterior lighting (bulbs, fixtures)", LIGHT, 7, "exterior lighting coach light bulb burnt out fixture yellow color flashing mismatched",
    "Exterior lighting at [location] ([describe: burnt-out or damaged bulb / mismatched bulbs / colored or flashing bulbs / damaged, broken or missing fixture]) does not comply with the Community Standards.",
    "Replace damaged or burnt-out bulbs within one week; use matching clear or frosted bulbs of the same wattage and lumens (no colored or flashing bulbs outside the winter holiday season); repair or replace damaged fixtures with matching bronze or black fixtures upon notice.")
add("3.24", "3.24", "Garden / walkway lighting", LIGHT, 15, "garden light walkway landscape lighting low voltage wires",
    "Garden/walkway lighting at [location] was installed without approval or does not meet the standards (height, bulb, wires visible, placement).",
    "Remove the lights or obtain ACC approval. Lights must be in existing beds, 12 inches or less, with white 4/7/11 watt bulbs, concealed wires, not reflecting into adjacent homes.")
add("3.25", "3.25", "Generator", LIGHT, 15, "generator propane standby",
    "A generator was observed at [location] without written approval, or [used outside a power outage / unscreened].",
    "Remove the generator or obtain written approval, permits and screening. Permanent propane generators may be used only during power outages.")
add("3.29", "3.29", "Holiday decorations left up", LIGHT, 7, "holiday decorations christmas halloween lights inflatable removed after season extension cords",
    "[Holiday/seasonal] decorations ([describe]) were observed on [date], outside the permitted display period.",
    "Remove the decorations, lights, extension cords and clips. Decorations may be displayed no earlier than 15 days before and must be removed no later than 7 days after a holiday; fall decorations Oct 1 - Dec 1; winter decorations 3 weeks before Thanksgiving until Jan 15. Light displays may not emit sound.")
add("3.30", "3.30", "House numbers missing or non-compliant", LIGHT, 14, "house number missing address numbers",
    "House numbers are [missing / not clearly readable from the street / not black, non-reflective and 4-6 inches].",
    "Install black, non-reflective house numbers 4 to 6 inches in height, clearly readable from the street, within 14 days of notice.")
add("3.11", "3.11", "Painted curb numbers", LIGHT, 15, "curb numbers painted address",
    "Numbers were painted on the curb at [location]. Painted curb numbers are not permitted.",
    "Remove the painted numbers and restore the curb to its original condition.")
add("3.47", "3.47", "Security lighting", LIGHT, 15, "security light floodlight motion night nuisance",
    "Security lighting at [location] [was installed without meeting the standards / was left on through the night / illuminates neighboring homes].",
    "Correct the lighting: simple lamp holders under the eave only (no wall mounting), motion-activated, aimed away from neighbors, and not left on throughout the night; or remove them.")
add("3.70", "3.70", "Eave / soffit lighting", LIGHT, 7, "eave soffit lighting govee hue led strip lights colors dangling cords",
    "Eave/soffit lighting at [location] [lacks ACC approval / has dangling cords or visible wires / is colored or on past 10 p.m. outside holiday periods].",
    "Obtain written ACC approval, secure all wires and cords flush and concealed (dangling cords within seven days), keep lights white outside holidays, and turn them off by 10 p.m.")

# ---- Signs, flags & events --------------------------------------------------------------------
add("3.48", "3.48", "Sign, flag or banner", SIGN, 7, "sign flag banner flagpole political for sale advertisement lettering",
    "A [sign / flag / banner] ([describe]) was observed at [location], visible from the outside.",
    "Remove the item. Only the permitted U.S., Florida and military/POW-MIA flags (up to 4.5' x 6') and one compliant flagpole (up to 20 feet) are allowed.")
add("3.10", "3.10", "Yard sale / personal event", SIGN, 7, "yard sale garage sale event signs",
    "A personal yard sale/event was observed [or advertised with signs] at [location] on [date].",
    "Cease the sale and remove any signage. Personal events are not permitted; the community holds two events each year.")

# ---- Pets & nuisance --------------------------------------------------------------------------
add("3.02", "3.02", "Pet rule violation", PETS, 7, "pet dog cat animal leash waste poop tied out barking dog run unattended animals",
    "[Describe: pet off leash / pet tied out or left unattended outside / pet waste not picked up / more than two pets / dog run, pen or doghouse / pet in a water body] at [location] on [date].",
    "Comply with Section 3.02: keep pets on a leash or within an enclosed yard, never tie them out or leave them unattended outside, keep no more than two pets, remove any dog run/pen/house, pick up all pet waste immediately, and keep pets out of water bodies.")
add("3.34", "3.34", "Nuisance", PETS, 7, "nuisance noise odor unsightly offensive disturbance",
    "[Describe the nuisance activity or condition] was observed at [location] on [date(s)].",
    "Cease the activity or correct the condition that constitutes a nuisance to neighbors.")
add("3.42", "3.42", "Rental of home (see Declaration)", MISC, 15, "rental rent tenant lease airbnb short term vacation",
    "[Describe the observed rental activity].",
    "Comply with the rental provisions of the Declaration. (Section 3.42 refers to the Declaration: confirm the specific requirement before issuing.)")

# ---- Recreation & play ------------------------------------------------------------------------
add("3.06", "3.06", "Basketball hoop", REC, 15, "basketball hoop backboard rim net portable street hoop",
    "A basketball hoop at [location] [lacks ACC approval / is one of more than one / is wall mounted / is in disrepair (peeling paint, rust, damaged backboard, yellowing, damaged rim or net) / is used in the street or after 9 p.m. / is weighted with rocks or bricks].",
    "Correct the issue: submit an ACC request (one hoop only, no wall mounting, backboard no larger than 60 inches), repair or replace a hoop in disrepair within 7 days, do not use it in the street or after 9 p.m., and fill the base only per the manufacturer's instructions.")
add("3.07", "3.07", "Bird house / feeder", REC, 15, "bird house birdhouse feeder",
    "A bird house/feeder at [location] [is not approved / is not in the rear yard / exceeds the size or height limits / is not maintained].",
    "Remove the bird house/feeder, or bring it into compliance (one only, rear yard, at least 5 feet from the property line, no higher than 6 feet, within size limits) with ACC approval.")
add("3.08", "3.08", "Clothesline", REC, 7, "clothesline clothes line drying",
    "A clothesline at [location] is visible from the street or neighbors or remains up when not in use.",
    "Remove the clothesline when not in use and keep it in the rear of the lot out of view of the street and neighbors.")
add("3.37", "3.37", "Play structure", REC, 14, "play structure swing set playset canopy torn jungle gym",
    "A play structure at [location] [lacks ACC approval / exceeds 12 feet / is made of metal tubing / has a ripped or torn canopy / is not maintained / is not buffered].",
    "Correct the issue: obtain ACC approval, maintain the structure, and replace torn canopies with an approved earth-tone canopy within 14 days. Play sets may not exceed 12 feet and must be wood or recycled plastic.")
add("3.51", "3.51", "Sports / recreational equipment", REC, 15, "sports equipment recreational playground skateboard ramp tree platform goal",
    "[Sports / recreational equipment] ([describe]) was observed at [location] without ACC approval or left outside visible from the street.",
    "Remove the equipment, or obtain ACC approval and buffer it with approved landscaping/fencing. Temporary sports equipment must be stored inside when not in use.")
add("3.58", "3.58", "Trampoline", REC, 15, "trampoline net netting",
    "A trampoline at [location] [lacks ACC approval / is visible from the street or neighbors / is not secured or has damaged netting].",
    "Obtain ACC approval, locate the trampoline in the rear where it is not visible from any street or neighboring home, secure it, and repair damaged netting.")

# ---- Landscaping & lawn -----------------------------------------------------------------------
add("3.03", "3.03", "Artificial vegetation", LAND, 15, "artificial grass turf plants flowers fake plastic",
    "Artificial [grass / plants / flowers] was observed at [location].",
    "Remove all artificial grass, plants and flowers from the exterior of the lot. Artificial turf and plastic or artificial flowers are not permitted.")
add("3.20", "3.20", "Fruit tree", LAND, 15, "fruit tree citrus orange lemon mango fruit bearing",
    "A fruit-bearing tree ([type]) was observed at [location].",
    "Remove the fruit-bearing tree. Fruit-bearing trees of any kind are not permitted.")
add("3.39", "3.39", "Potted plants", LAND, 15, "potted plants flowerpot pots empty weeds driveway front",
    "Potted plants at [location] [exceed the limits (3 in the driveway area / 5 in front) / are empty, bare or contain weeds / are not maintained].",
    "Reduce the number of pots to the permitted limits, and keep every pot planted with approved, healthy plants and free of weeds.")
add("3.61", "3.61", "Vegetable garden", LAND, 15, "vegetable garden fruit vegetables plants raised bed",
    "A vegetable/fruit garden at [location] [lacks ACC approval / is not in the rear of the home / is not screened from view / has plants over 6 feet or weeds].",
    "Remove the garden or obtain ACC approval; gardens must be in the back of the home, screened from view, maintained, weeded, and no plants may exceed 6 feet.")
add("3.62", "3.62", "Obstruction of visibility on corner", LAND, 7, "visibility corner intersection sight line obstruction hedge",
    "A [hedge / shrub / object] at [location] obstructs visibility at the street intersection.",
    "Trim or remove the obstruction so that sight lines for drivers and pedestrians are clear (plants within 10 feet of a driveway edge or intersection must not exceed 30 inches).")
add("3.32", "3.32", "Irrigation staining / watering schedule", LAND, 15, "irrigation sprinkler staining rust watering schedule water restrictions",
    "[Staining caused by irrigation on a home, structure or paved area / watering outside the Sarasota County schedule] was observed at [location].",
    "Treat and remove irrigation staining, and follow the Sarasota County watering schedule.")
add("3.66", "3.66", "Wetland / conservation area vegetation removed", LAND, 7, "wetland conservation vegetation cut removal spraying swfwmd preserve",
    "Vegetation in the wetland/conservation area behind [address] appears to have been [cut / sprayed / removed].",
    "Cease cutting, spraying or removing vegetation in wetland or conservation areas. Contact the Association. The homeowner is responsible for any resulting fine from SWFWMD once the responsible party is established.")
add("3.12", "3.12", "Gutters / drainage", MAINT, 15, "gutter downspout drainage fascia soffit stained debris water runoff",
    "[Gutters/fascia/soffits stained or with debris / a downspout directing water onto common area or a neighboring property] at [location].",
    "Clean the gutters, fascia and soffits, and redirect downspouts so they do not direct water onto common areas or neighboring property.")
add("3.13", "3.13", "Driveway condition", MAINT, 15, "driveway stain grease oil weeds pavers debris reflectors surface",
    "The driveway at [location] has [oil/grease/other stains / weeds growing through the pavers / debris / unapproved surface application / reflectors].",
    "Clean the stains, remove weeds and debris, and remove any reflectors or unapproved surface applications. Driveway changes require ACC approval.")
add("3.35", "3.35", "Pressure cleaning needed", MAINT, 30, "pressure clean wash mold mildew algae dirty roof sidewalk driveway house stained",
    "The [roof / exterior walls / driveway / sidewalk / lanai] at [location] is [stained / covered in mold, mildew or algae] and needs cleaning.",
    "Pressure clean the roof and exterior surfaces and pavement, including sidewalks and driveways, within 30 days of notice.")
add("4.02-a", "4.02", "Weeds in landscape beds / planters", LAND, 15,
    "weeds weed weeding landscape bed beds planter planters flower bed flowerbed garden mulch overgrown unweeded unsightly growth",
    "The landscape beds / planters at [location] contain weeds or other unsightly growth.",
    "Remove the weeds and other unsightly growth from all landscape beds and planters, and keep them weeded and maintained.")
add("4.05-w", "4.05", "Weeds in lawn / turf", LAND, 15,
    "weeds weed lawn turf grass yard weedy crabgrass dollarweed broadleaf lawn weeds weed free",
    "The lawn at [location] has weeds that are not under control.",
    "Remove the weeds from the lawn and keep it weed-free through regular weeding and lawn care.")
add("4.02-c", "4.02", "Landscape change without approval", LAND, 15, "landscaping change tree removed planted bed ACC approval shrub removal",
    "A landscape change ([describe: tree added or removed / shrub or hedge over 3 feet planted / landscape bed changed]) was made at [location] without ACC approval.",
    "Submit an ACC application for the change, or restore the landscaping to its previous approved condition.")
add("4.02-d", "4.02", "Lawn maintenance / hard edges", LAND, 15, "lawn mow mowing grass clippings hard edge edging sidewalk weeds dead sod overgrown",
    "The lawn at [location] is [not regularly mowed / overgrown / not hard-edged along sidewalks, curbs and driveways / has dead or damaged sod / has grass clippings left on hard surfaces].",
    "Mow the lawn, hard-edge it along the sidewalks, curbs and driveways, remove grass clippings from driveway, sidewalk and street, and replace damaged or dead sod.")
add("4.02-e", "4.02", "Failing or dead plant", LAND, 30, "dead plant shrub failing dying brown yellow hedge replace",
    "[Plant(s) at (location)] are dead or failing (dead, lost 50% or more of foliage, predominantly brown or yellow, or no longer screening as required).",
    "Remove and replace each failing plant with a healthy plant of comparable type and size within 30 days of notice. If a plant is recovering from recent pruning, storm damage or transplanting, you may request up to 90 days for recovery.")
add("4.02-f", "4.02", "Shrubs / hedges overgrown", LAND, 15, "hedge shrub overgrown pruning trimming height 48 inches front yard untrimmed",
    "[Shrubs / hedges] at [location] are overgrown or untrimmed and exceed the maximum height (48 inches in the front yard, 10 feet in side and rear yards) or encroach on a sidewalk, driveway or street.",
    "Prune the plants to the permitted height and shape and keep them off sidewalks, driveways and streets. Hedges over 3 feet must be pruned at least twice per year.")
add("4.02-g", "4.02", "Tree maintenance / dead tree / stump", LAND, 30, "tree dead damaged leaning topped topping stump support stakes hazard tree removal arborist",
    "[Describe: dead or failing tree / topped tree / tree supports left on more than 12 months / stump not removed / leaning or hazardous tree] at [location].",
    "Remove and replace a failing tree with an approved species within 30 days of notice (or submit a certified arborist evaluation within 30 days), remove tree supports after 12 months, and grind stumps and restore the area within 60 days of removal. A hazardous tree must be made safe immediately.")
add("4.02-h", "4.02", "Prohibited / invasive plants", LAND, 15, "invasive plant brazilian pepper melaleuca prohibited plants exhibit",
    "A prohibited or invasive plant ([species]) was observed at [location].",
    "Remove the prohibited plant. Invasive plants, including Brazilian pepper, melaleuca and any Category I species, are not permitted anywhere in Heron Landing.")
add("4.03", "4.03", "Landscape edging", LAND, 15, "landscape edging bed edge tree ring plastic wood mixed styles",
    "The landscape edging at [location] [is missing / is not an approved type or color / is damaged or rising out of the ground / is inconsistent / lacks tree rings].",
    "Install or repair edging in an approved type and color (concrete curbing, stacked brick/block/rock, properly installed black rubber tube-type, terrace board or aluminum), consistent across the property, with tree rings. No edging between the grass and driveway pavers.")
add("4.04", "4.04", "Bed coverings / mulch", LAND, 15, "mulch rock bed covering bare ground pine straw",
    "Landscape beds at [location] have [bare ground / less than a 2-inch layer of mulch or rock / a non-approved bed covering].",
    "Refresh beds with at least a 2-inch layer of approved mulch (cypress, pine bark or cedar in red, black or brown) or approved rock. Shells, crushed gravel, pea gravel and coral are not permitted. Refresh mulch at least annually.")
add("4.05", "4.05", "Turf standards", LAND, 15, "turf grass lawn height weeds dead patchy bare st augustine artificial clumps",
    "The turf at [location] [is patchy or has bare areas / has weeds / is yellow or brown / is not mowed at 3-4 inches / has clippings / is not St. Augustine].",
    "Restore the lawn to the standards: healthy green St. Augustine grass, mowed at 3-4 inches, weed-free, pest and disease free, hard-edged, with no visible clumps of clippings.")

# 4.02 sub-sections keep their own slice of the verbatim text
SUB = {"4.02-a": "General Standard", "4.02-c": "Approval of Changes", "4.02-d": "Lawns and Turf", "4.02-e": "Plant Health and Replacement",
       "4.02-f": "Shrubs, Hedges", "4.02-g": "Trees", "4.02-h": "Prohibited Plants"}
SUB_ORDER = ["General Standard", "Definitions", "Approval of Changes", "Lawns and Turf", "Plant Health and Replacement",
             "Shrubs, Hedges", "Trees.", "Prohibited Plants", "Determinations and Compliance", "Existing Landscaping"]


def split_402(text):
    marks = []
    for name in SUB_ORDER:
        m = re.search(r"\(([a-j])\) " + re.escape(name), text)
        if m:
            marks.append((m.start(), name))
    marks.sort()
    out = {}
    for i, (pos, name) in enumerate(marks):
        out[name.rstrip(".")] = text[pos:marks[i + 1][0] if i + 1 < len(marks) else len(text)].strip()
    return out


def main(txt_path):
    parsed = parse(Path(txt_path).read_text(encoding="utf8"))
    by_section = {s["section"]: s for s in parsed}
    subs = split_402(by_section["4.02"]["rule"])
    items = []
    for d in D:
        base = by_section[d["section"]]
        rule = base["rule"]
        if d["code"] in SUB:
            rule = subs[SUB[d["code"]]]
        if d["code"] == "4.05-w":   # quote just the weed clauses of the turf standard
            m = re.search(r"No Weeds:.*?taking over\.", rule, flags=re.S)
            assert m, "4.05 No Weeds clause not found"
            rule = "Turf must be maintained & trimmed in a professional looking manner. " + re.sub(r"\s+", " ", m.group(0))
        items.append({**d, "standards_title": base["title"], "rule": rule})
    missing = [d["code"] for d in D if d["section"] not in by_section]
    assert not missing, missing
    assert len({d["code"] for d in D}) == len(D), "duplicate codes"
    out = Path(__file__).resolve().parent.parent / "violations" / "data" / "standards_catalog.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"source": "Heron Landing Community Standards, Seventh Edition (October 2026)",
                               "items": items}, indent=1, ensure_ascii=False), encoding="utf8")
    print(f"wrote {len(items)} items to {out}")


def parse(raw):
    lines = raw.split("\n")
    start = next(i for i, l in enumerate(lines) if re.match(r"\s*1\.01\s+Responsibilities\s*$", l))
    foot = re.compile(r"Heron Landing Community Standards\s+\d+\s+October 2026")
    text = "\n".join(l.strip() for l in lines[start:] if not foot.search(l)).split("Appendix A")[0]
    head = re.compile(r"^(\d\.\d\d)\s+([A-Z][^\n]*?)\s*$", re.M)
    ms = list(head.finditer(text))
    secs = []
    for i, m in enumerate(ms):
        chunk = text[m.end(): ms[i + 1].start() if i + 1 < len(ms) else len(text)]
        chunk = re.sub(r"\n\s*ARTICLE \d.*", "", chunk, flags=re.S)
        paras = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", chunk)]
        secs.append({"section": m.group(1), "title": m.group(2).strip(), "rule": "\n\n".join(p for p in paras if p)})
    return secs


if __name__ == "__main__":
    main(sys.argv[1])
