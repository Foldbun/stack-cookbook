"""Exclusion lists for the Open Images subsets: people, body parts, clothing, plates, flags, logos, weapons,
alcohol, medical scenes, vehicles, sculptures, screens. Taken verbatim from the metadata filter of an earlier
selection job. Explicit names are checked against the class table at build time (unknown names only warn)."""
import re

EXPLICIT_RAW = """Person;Man;Woman;Boy;Girl;Human face;Human head;Human hair;Human eye;Human mouth;Human ear;Human nose;
Human hand;Human arm;Human leg;Human foot;Human body;Beard;Baby;Child;Toddler;Infant bed;Diaper;Doll;Sculpture;Bronze sculpture;Bust;
Statue;Mannequin;Poster;Television;Computer monitor;Laptop;Tablet computer;Mobile phone;Telephone;Corded phone;Ipod;Billboard;Scoreboard;
Whiteboard;Picture frame;Mirror;Toy;Teddy bear;Snowman;Skull;Cowboy hat;Fedora;Hat;Sun hat;Sombrero;Helmet;Bicycle helmet;Football helmet;
Crown;Tiara;Goggles;Glasses;Sunglasses;Headphones;Earring;Necklace;Watch;Tie;Scarf;Belt;Glove;Sock;Footwear;Boot;Sandal;High heels;
Clothing;Coat;Jacket;Jeans;Dress;Shirt;Shorts;Skirt;Miniskirt;Suit;Trousers;Sports uniform;Swimwear;Swim cap;Brassiere;
Personal flotation device;Backpack;Handbag;Luggage and bags;Suitcase;Briefcase;Umbrella_PLACEHOLDER;Fashion accessory;Cosmetics;Lipstick;
Face powder;Perfume;Hair spray;Hair dryer;Wheelchair;Stretcher;Crutch;Medical equipment;Syringe;Stethoscope;Band-aid;
Weapon;Sword;Dagger;Handgun;Rifle;Shotgun;Bomb;Missile;Cannon;Tank;Bow and arrow;Axe;Chainsaw;Knife;Kitchen knife;
Beer;Wine;Wine glass;Wine rack;Cocktail;Cocktail shaker;Drink;Bottle;Flag;Vehicle registration plate;Traffic sign;Stop sign;
Car;Taxi;Limousine;Van;Truck;Bus;Golf cart;Segway;Snowmobile;Unicycle;Motorcycle;Ambulance;Snowplow;Parachute;Jet ski;Surfboard;Ski;Snowboard;Skateboard;
Roller skates;Racket;Tennis racket;Table tennis racket;Baseball bat;Baseball glove;Football;Rugby ball;Volleyball (Ball);Tennis ball;Golf ball;
Cricket ball;Ball (Object);Balance beam;Horizontal bar;Treadmill;Indoor rower;Training bench;Stationary bicycle;Punching bag;Dumbbell;
Swimming pool;Jacuzzi;Bathtub;Shower;Toilet;Bidet;Bed;Pillow;Maraca;Guitar;Violin;Cello;Piano;Drum;Microphone;Camera;Binoculars;Tripod;
Christmas tree;Tent;Hiking equipment;Sports equipment;Flying disc""".replace('\n', '').split(';')
EXPLICIT = [e.strip() for e in EXPLICIT_RAW if e.strip() and "PLACEHOLDER" not in e]

KEYWORDS = re.compile(r"\b(person|people|persons|man|men|woman|women|boy|girl|child|children|kid|kids|baby|babies|infant|toddler|human|face|head|hair|"
 r"hairstyle|eye|eyes|eyebrow|eyelash|mouth|lip|lips|ear|nose|hand|hands|finger|thumb|arm|leg|foot|feet|toe|neck|shoulder|chin|cheek|jaw|forehead|"
 r"tooth|teeth|skin|beard|moustache|mustache|crowd|audience|selfie|portrait|smile|smiling|laugh|wedding|bride|groom|cosplay|dance|dancer|dancing|"
 r"party|costume|mask|athlete|player|team|soldier|army|military|police|firefighter|priest|monk|nun|santa|clown|model|fashion|lady|gentleman|"
 r"friend|friends|family|couple|mother|father|son|daughter|brother|sister|student|teacher|worker|tourist|passenger|rider|cyclist|"
 r"pedestrian|fun|leisure|recreation|event|ceremony|festival|parade|protest|rally|concert|performance|performer|singer|musician|"
 r"statue|sculpture|mannequin|puppet|doll|figurine|action figure|cartoon|anime|comics|manga|poster|billboard|screenshot|text|font|"
 r"handwriting|logo|brand|advertising|meme|collage|swimwear|bikini|lingerie|underwear|nudity|nude|tattoo|"
 r"gun|firearm|weapon|knife|sword|cigarette|cigar|tobacco|smoking|smoke|alcohol|beer|wine|liquor|cocktail|whisky|whiskey|vodka|drug|"
 r"cross|crucifix|church|temple|mosque|synagogue|shrine|religion|religious|buddha|jesus|mary|god|flag|swastika|political|election|"
 r"license plate|vehicle registration plate|number plate|medical|hospital|surgery|syringe|pill|clothing|apparel|uniform|jacket|coat|shirt|dress|"
 r"hat|cap|helmet|glasses|sunglasses|shoe|shoes|boot|boots|sandal|sock|glove|bag|backpack|handbag)\b", re.I)


# Use case 06 (product photos) lets some product-like classes back in. They are excluded for 01 / 02 / 05 because photos of
# them often show people; for 06 the person-related names, the keyword filter for body parts and the machine-label check stay
# as they are, so an image only qualifies when nothing about a person is annotated or predicted on it.
PRODUCT_ALLOWED = {"Toy", "Bottle", "Watch", "Handbag", "Backpack", "Luggage and bags", "Suitcase", "Briefcase", "Footwear",
                   "Boot", "Sandal", "High heels", "Umbrella", "Fashion accessory", "Camera",
                   "Teddy bear", "Bed", "Pillow", "Guitar", "Violin", "Cello", "Piano", "Drum",
                   "Sunglasses", "Glasses", "Hat", "Sun hat", "Helmet", "Bicycle helmet", "Scarf", "Tie", "Necklace",
                   "Earring", "Belt", "Glove", "Sock", "Perfume", "Cosmetics", "Ball (Object)",
                   "Football", "Tennis ball", "Racket", "Tennis racket", "Binoculars", "Tripod",
                   "Headphones", "Microphone", "Maraca",
                   "Baseball glove", "Baseball bat", "Flying disc", "Sports equipment", "Hair dryer"}
