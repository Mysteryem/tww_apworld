import os
from dataclasses import fields

import yaml

from BaseClasses import LocationProgressType, MultiWorld, Region, Tutorial
from Fill import fill_restrictive
from worlds.AutoWorld import WebWorld, World
from worlds.generic.Rules import add_item_rule
from worlds.LauncherComponents import (
    Component,
    SuffixIdentifier,
    Type,
    components,
    launch_subprocess,
)

from .Items import ISLAND_NUMBER_TO_CHART_NAME, ITEM_TABLE, TWWItem
from .Locations import (
    DUNGEON_NAMES,
    ISLAND_NUMBER_TO_NAME,
    LOCATION_TABLE,
    VANILLA_DUNGEON_ITEM_LOCATIONS,
    TWWFlag,
    TWWLocation,
    split_location_name_by_zone,
)
from .Macros import *
from .Options import TWWOptions
from .Regions import *
from .Rules import set_rules


def run_client():
    print("Running TWW Client")
    from .TWWClient import main  # lazy import

    launch_subprocess(main, name="WindWakerClient")


components.append(
    Component(
        "TWW Client",
        func=run_client,
        component_type=Type.CLIENT,
        file_identifier=SuffixIdentifier(".aptww"),
    )
)


class TWWWeb(WebWorld):
    theme = "ocean"
    tutorials = [
        Tutorial(
            "Multiworld Setup Guide",
            "A guide to setting up the Archipelago The Wind Waker software on your computer.",
            "English",
            "setup_en.md",
            "setup/en",
            ["tanjo3", "Lunix"],
        )
    ]


class TWWWorld(World):
    """
    Legend has it that whenever evil has appeared, a hero named Link has arisen to defeat it. The legend continues on
    the surface of a vast and mysterious sea as Link sets sail in his most epic, awe-inspiring adventure yet. Aided by a
    magical conductor's baton called the Wind Waker, he will face unimaginable monsters, explore puzzling dungeons, and
    meet a cast of unforgettable characters as he searches for his kidnapped sister."""

    game: str = "The Wind Waker"
    options_dataclass = TWWOptions
    options: TWWOptions

    topology_present: bool = True

    item_name_to_id: dict[str, int] = {
        name: TWWItem.get_apid(data.code) for name, data in ITEM_TABLE.items() if data.code is not None
    }
    location_name_to_id: dict[str, int] = {
        name: TWWLocation.get_apid(data.code) for name, data in LOCATION_TABLE.items() if data.code is not None
    }

    item_name_groups = {
        "Pearls": {
            "Nayru's Pearl",
            "Din's Pearl",
            "Farore's Pearl",
        },
        "Shards": {
            "Triforce Shard 1",
            "Triforce Shard 2",
            "Triforce Shard 3",
            "Triforce Shard 4",
            "Triforce Shard 5",
            "Triforce Shard 6",
            "Triforce Shard 7",
            "Triforce Shard 8",
        },
        "Tingle Statues": {
            "Dragon Tingle Statue",
            "Forbidden Tingle Statue",
            "Goddess Tingle Statue",
            "Earth Tingle Statue",
            "Wind Tingle Statue",
        },
    }

    web = TWWWeb()

    def __init__(self, *args, **kwargs):
        super(TWWWorld, self).__init__(*args, **kwargs)

        self.itempool: list[TWWItem] = []
        self.pre_fill_items: list[TWWItem] = []

        self.vanilla_dungeon_item_names: set[str] = set()
        self.own_dungeon_item_names: set[str] = set()
        self.any_dungeon_item_names: set[str] = set()

        self.island_number_to_chart_name = ISLAND_NUMBER_TO_CHART_NAME.copy()

        self.required_boss_item_locations: list[str] = []
        self.required_dungeons: list[str] = []
        self.banned_dungeons: list[str] = []

    def _get_access_rule(self, region):
        snake_case_region = region.lower().replace("'", "").replace(" ", "_")
        return f"can_access_{snake_case_region}"

    def _get_dungeon_locations(self):
        dungeon_regions = DUNGEON_EXITS.copy()

        # If miniboss entrances are not shuffled, include miniboss arenas as a dungeon regions.
        if not self.options.randomize_miniboss_entrances:
            dungeon_regions += [
                "Forbidden Woods Miniboss Arena",
                "Tower of the Gods Miniboss Arena",
                "Earth Temple Miniboss Arena",
                "Wind Temple Miniboss Arena",
            ]

        # Forsaken Fortress is odd as it exists on the Great Sea.
        # Simply keep a list of all locations in the dungeon, except the boss Heart Container.
        ff_dungeon_locations = [
            "Forsaken Fortress - Phantom Ganon",
            "Forsaken Fortress - Chest Outside Upper Jail Cell",
            "Forsaken Fortress - Chest Inside Lower Jail Cell",
            "Forsaken Fortress - Chest Guarded By Bokoblin",
            "Forsaken Fortress - Chest on Bed",
        ]

        return [
            location
            for location in self.multiworld.get_locations(self.player)
            if location.name in ff_dungeon_locations or location.region in dungeon_regions
        ]

    def _randomize_charts(self):
        # This code comes straight from the base randomizer's chart randomizer.

        original_item_names = list(self.island_number_to_chart_name.values())

        # Shuffles the list of island numbers.
        # The shuffled island numbers determine which sector each chart points to.
        shuffled_island_numbers = list(self.island_number_to_chart_name.keys())
        self.multiworld.random.shuffle(shuffled_island_numbers)

        for original_item_name in original_item_names:
            shuffled_island_number = shuffled_island_numbers.pop()
            self.island_number_to_chart_name[shuffled_island_number] = original_item_name

            # Properly adjust the flags for sunken treasure locations.
            island_name = ISLAND_NUMBER_TO_NAME[shuffled_island_number]
            island_location = self.multiworld.get_location(f"{island_name} - Sunken Treasure", self.player)
            if original_item_name.startswith("Triforce Chart "):
                island_location.flags = TWWFlag.TRI_CHT
            else:
                island_location.flags = TWWFlag.TRE_CHT

    def _randomize_required_bosses(self):
        dungeon_names = set(DUNGEON_NAMES)

        # Assert that the user is not including and excluding a dungeon at the same time.
        if len(self.options.included_dungeons.value & self.options.excluded_dungeons.value) != 0:
            raise RuntimeError("Conflict found in the lists of required and banned dungeons for required bosses mode")

        # If the user enforces a dungeon location to be priority, consider that when selecting required bosses.
        required_dungeons = self.options.included_dungeons.value
        for location_name in self.options.priority_locations.value:
            dungeon_name, _ = split_location_name_by_zone(location_name)
            if dungeon_name in dungeon_names:
                required_dungeons.add(dungeon_name)

        # Ensure that we aren't prioritizing more dungeon locations than requested number of required bosses.
        num_required_bosses = self.options.num_required_bosses
        if len(required_dungeons) > num_required_bosses:
            raise RuntimeError("Could not select required bosses to satisfy options set by user")

        # Ensure that after removing excluded dungeons that we still have enough dungeons to satisfy user options.
        num_remaining = num_required_bosses - len(required_dungeons)
        remaining_dungeon_options = dungeon_names - required_dungeons - self.options.excluded_dungeons.value
        if len(remaining_dungeon_options) < num_remaining:
            raise RuntimeError("Could not select required bosses to satisfy options set by user")

        # Finish selecting required bosses.
        required_dungeons.update(self.multiworld.random.sample(list(remaining_dungeon_options), num_remaining))

        # Exclude locations which are not in the dungeon of a required boss.
        banned_dungeons = dungeon_names - required_dungeons
        for location_name, _ in LOCATION_TABLE.items():
            dungeon_name, _ = split_location_name_by_zone(location_name)
            if dungeon_name in banned_dungeons:
                self.multiworld.get_location(location_name, self.player).progress_type = LocationProgressType.EXCLUDED

        # Exclude mail related to banned dungeons.
        if "Forbidden Woods" in banned_dungeons:
            self.multiworld.get_location("Mailbox - Letter from Orca", self.player).progress_type = (
                LocationProgressType.EXCLUDED
            )
        if "Forsaken Fortress" in banned_dungeons:
            self.multiworld.get_location("Mailbox - Letter from Aryll", self.player).progress_type = (
                LocationProgressType.EXCLUDED
            )
            self.multiworld.get_location("Mailbox - Letter from Tingle", self.player).progress_type = (
                LocationProgressType.EXCLUDED
            )
        if "Earth Temple" in banned_dungeons:
            self.multiworld.get_location("Mailbox - Letter from Baito", self.player).progress_type = (
                LocationProgressType.EXCLUDED
            )

        # Record the item location names for required bosses.
        possible_boss_item_locations = [loc for loc, data in LOCATION_TABLE.items() if TWWFlag.BOSS in data.flags]
        self.required_boss_item_locations = [
            loc for loc in possible_boss_item_locations if split_location_name_by_zone(loc)[0] in required_dungeons
        ]
        self.required_dungeons = list(required_dungeons)
        self.banned_dungeons = list(banned_dungeons)

    def _randomize_entrances(self):
        # Copy over the lists of entrances by type.
        entrances = [
            DUNGEON_ENTRANCES.copy(),
            MINIBOSS_ENTRANCES.copy(),
            BOSS_ENTRANCES.copy(),
            SECRET_CAVES_ENTRANCES.copy(),
            SECRET_CAVES_INNER_ENTRANCES.copy(),
            FAIRY_FOUNTAIN_ENTRANCES.copy(),
        ]
        exits = [
            DUNGEON_EXITS.copy(),
            MINIBOSS_EXITS.copy(),
            BOSS_EXITS.copy(),
            SECRET_CAVES_EXITS.copy(),
            SECRET_CAVES_INNER_EXITS.copy(),
            FAIRY_FOUNTAIN_EXITS.copy(),
        ]

        # Retrieve the entrance randomization option.
        options = [
            self.options.randomize_dungeon_entrances,
            self.options.randomize_miniboss_entrances,
            self.options.randomize_boss_entrances,
            self.options.randomize_secret_cave_entrances,
            self.options.randomize_secret_cave_inner_entrances,
            self.options.randomize_fairy_fountain_entrances,
        ]

        entrance_exit_pairs: list[tuple[Region, Region]] = []

        # Force miniboss doors to be vanilla in nonrequired dungeons.
        for miniboss_entrance, miniboss_exit in zip(entrances[1], exits[1]):
            assert miniboss_entrance.startswith("Miniboss Entrance in ")
            dungeon_name = miniboss_entrance[len("Miniboss Entrance in ") :]
            if dungeon_name in self.banned_dungeons:
                entrances[1].remove(miniboss_entrance)
                entrance_region = self.multiworld.get_region(miniboss_entrance, self.player)
                exits[1].remove(miniboss_exit)
                exit_region = self.multiworld.get_region(miniboss_exit, self.player)
                entrance_exit_pairs.append((entrance_region, exit_region))

        # Force boss doors to be vanilla in nonrequired dungeons.
        for boss_entrance, boss_exit in zip(entrances[2], exits[2]):
            assert boss_entrance.startswith("Boss Entrance in ")
            dungeon_name = boss_entrance[len("Boss Entrance in ") :]
            if dungeon_name in self.banned_dungeons:
                entrances[2].remove(boss_entrance)
                entrance_region = self.multiworld.get_region(boss_entrance, self.player)
                exits[2].remove(boss_exit)
                exit_region = self.multiworld.get_region(boss_exit, self.player)
                entrance_exit_pairs.append((entrance_region, exit_region))

        if self.options.mix_entrances == "separate_pools":
            # Connect entrances to exits of the same type.
            for option, entrance_group, exit_group in zip(options, entrances, exits):
                # If the entrance group is randomized, shuffle their order.
                if option:
                    self.multiworld.random.shuffle(entrance_group)
                    self.multiworld.random.shuffle(exit_group)

                for entrance_name, exit_name in zip(entrance_group, exit_group):
                    entrance_region = self.multiworld.get_region(entrance_name, self.player)
                    exit_region = self.multiworld.get_region(exit_name, self.player)
                    entrance_exit_pairs.append((entrance_region, exit_region))
        elif self.options.mix_entrances == "mix_pools":
            # We do a bit of extra work here in order to prevent unreachable "islands" of regions.
            # For example, DRC boss door leading to DRC. This will cause generation failures.

            # Gather all the entrances and exits for selected randomization pools.
            randomized_entrances: list[str] = []
            randomized_exits: list[str] = []
            non_randomized_exits: list[str] = ["The Great Sea"]
            for option, entrance_group, exit_group in zip(options, entrances, exits):
                if option:
                    randomized_entrances += entrance_group
                    randomized_exits += exit_group
                else:
                    # If not randomized, then just connect the entrance-exit pairs now.
                    for entrance_name, exit_name in zip(entrance_group, exit_group):
                        non_randomized_exits.append(exit_name)
                        entrance_region = self.multiworld.get_region(entrance_name, self.player)
                        exit_region = self.multiworld.get_region(exit_name, self.player)
                        entrance_exit_pairs.append((entrance_region, exit_region))

            # Build a list of accessible randomized entrances, assuming the player has all items.
            accessible_entrances: list[str] = []
            for exit_name, entrances in ENTRANCE_ACCESSIBILITY.items():
                if exit_name in non_randomized_exits:
                    accessible_entrances += [
                        entrance_name for entrance_name in entrances if entrance_name in randomized_entrances
                    ]
            non_accessible_entrances: list[str] = [
                entrance_name for entrance_name in randomized_entrances if entrance_name not in accessible_entrances
            ]

            # Priotize exits that lead to more entrances first.
            priority_exits: list[str] = []
            for exit_name, entrances in ENTRANCE_ACCESSIBILITY.items():
                if exit_name == "The Great Sea":
                    continue
                if exit_name in randomized_exits and any(
                    entrance_name in randomized_entrances for entrance_name in entrances
                ):
                    priority_exits.append(exit_name)

            # Assign each priority exit to an accessible entrance.
            for exit_name in priority_exits:
                # Choose an accessible entrance at random.
                self.multiworld.random.shuffle(accessible_entrances)
                entrance_name = accessible_entrances.pop()

                # Connect the pair.
                entrance_region = self.multiworld.get_region(entrance_name, self.player)
                exit_region = self.multiworld.get_region(exit_name, self.player)
                entrance_exit_pairs.append((entrance_region, exit_region))

                # Remove the pair from the list of entrance/exits to be connected.
                randomized_entrances.remove(entrance_name)
                randomized_exits.remove(exit_name)

                # Consider entrances in that exit as accessible now.
                for newly_accessible_entrance in ENTRANCE_ACCESSIBILITY[exit_name]:
                    if newly_accessible_entrance in non_accessible_entrances:
                        accessible_entrances.append(newly_accessible_entrance)
                        non_accessible_entrances.remove(newly_accessible_entrance)

            # With all entrances either assigned or accessible, we should have an equal number of unassigned entrances
            # and exits to pair.
            assert len(randomized_entrances) == len(randomized_exits)

            # Join the remaining entrance/exits randomly.
            self.multiworld.random.shuffle(randomized_entrances)
            self.multiworld.random.shuffle(randomized_exits)
            for entrance_name, exit_name in zip(randomized_entrances, randomized_exits):
                entrance_region = self.multiworld.get_region(entrance_name, self.player)
                exit_region = self.multiworld.get_region(exit_name, self.player)
                entrance_exit_pairs.append((entrance_region, exit_region))
        else:
            raise Exception(f"Invalid entrance randomization option: {self.options.mix_entrances}")

        return entrance_exit_pairs

    def _set_nonprogress_locations(self):
        enabled_flags = TWWFlag.ALWAYS

        # Set the flags for progression location by checking player's settings.
        if self.options.progression_dungeons:
            enabled_flags |= TWWFlag.DUNGEON
        if self.options.progression_tingle_chests:
            enabled_flags |= TWWFlag.TNGL_CT
        if self.options.progression_dungeon_secrets:
            enabled_flags |= TWWFlag.DG_SCRT
        if self.options.progression_puzzle_secret_caves:
            enabled_flags |= TWWFlag.PZL_CVE
        if self.options.progression_combat_secret_caves:
            enabled_flags |= TWWFlag.CBT_CVE
        if self.options.progression_savage_labyrinth:
            enabled_flags |= TWWFlag.SAVAGE
        if self.options.progression_great_fairies:
            enabled_flags |= TWWFlag.GRT_FRY
        if self.options.progression_short_sidequests:
            enabled_flags |= TWWFlag.SHRT_SQ
        if self.options.progression_long_sidequests:
            enabled_flags |= TWWFlag.LONG_SQ
        if self.options.progression_spoils_trading:
            enabled_flags |= TWWFlag.SPOILS
        if self.options.progression_minigames:
            enabled_flags |= TWWFlag.MINIGME
        if self.options.progression_battlesquid:
            enabled_flags |= TWWFlag.SPLOOSH
        if self.options.progression_free_gifts:
            enabled_flags |= TWWFlag.FREE_GF
        if self.options.progression_platforms_rafts:
            enabled_flags |= TWWFlag.PLTFRMS
        if self.options.progression_submarines:
            enabled_flags |= TWWFlag.SUBMRIN
        if self.options.progression_eye_reef_chests:
            enabled_flags |= TWWFlag.EYE_RFS
        if self.options.progression_big_octos_gunboats:
            enabled_flags |= TWWFlag.BG_OCTO
        if self.options.progression_triforce_charts:
            enabled_flags |= TWWFlag.TRI_CHT
        if self.options.progression_treasure_charts:
            enabled_flags |= TWWFlag.TRE_CHT
        if self.options.progression_expensive_purchases:
            enabled_flags |= TWWFlag.XPENSVE
        if self.options.progression_island_puzzles:
            enabled_flags |= TWWFlag.ISLND_P
        if self.options.progression_misc:
            enabled_flags |= TWWFlag.MISCELL

        for location in self.multiworld.get_locations(self.player):
            # If not all the flags for a location are set, then force that location to have a non-progress item.
            if location.flags & enabled_flags != location.flags:
                location.progress_type = LocationProgressType.EXCLUDED

    def generate_early(self):
        # Handle randomization options for dungeon items.
        for item, data in ITEM_TABLE.items():
            match data.type:
                case "Map" | "Compass":
                    if self.options.randomize_mapcompass == "startwith":
                        self.options.start_inventory.value[item] = data.quantity
                    elif self.options.randomize_mapcompass == "vanilla":
                        self.vanilla_dungeon_item_names.add(item)
                    elif self.options.randomize_mapcompass == "dungeon":
                        self.own_dungeon_item_names.add(item)
                    elif self.options.randomize_mapcompass == "any_dungeon":
                        self.any_dungeon_item_names.add(item)
                    elif self.options.randomize_mapcompass == "local":
                        self.options.local_items.value.add(item)

                case "SKey":
                    if self.options.randomize_smallkeys == "startwith":
                        self.options.start_inventory.value[item] = data.quantity
                    elif self.options.randomize_smallkeys == "vanilla":
                        self.vanilla_dungeon_item_names.add(item)
                    elif self.options.randomize_smallkeys == "dungeon":
                        self.own_dungeon_item_names.add(item)
                    elif self.options.randomize_smallkeys == "any_dungeon":
                        self.any_dungeon_item_names.add(item)
                    elif self.options.randomize_smallkeys == "local":
                        self.options.local_items.value.add(item)

                case "BKey":
                    if self.options.randomize_bigkeys == "startwith":
                        self.options.start_inventory.value[item] = data.quantity
                    elif self.options.randomize_bigkeys == "vanilla":
                        self.vanilla_dungeon_item_names.add(item)
                    elif self.options.randomize_bigkeys == "dungeon":
                        self.own_dungeon_item_names.add(item)
                    elif self.options.randomize_bigkeys == "any_dungeon":
                        self.any_dungeon_item_names.add(item)
                    elif self.options.randomize_bigkeys == "local":
                        self.options.local_items.value.add(item)

        # If sword mode is Start with Hero's Sword, then send the player a starting sword.
        if self.options.sword_mode == "start_with_sword":
            self.options.start_inventory.value["Progressive Sword"] = (
                self.options.start_inventory.value.get("Progressive Sword", 0) + 1
            )

        # If sword mode is Swordless, ensure the player does not start with a sword.
        if self.options.sword_mode == "swordless":
            self.options.start_inventory.value["Progressive Sword"] = 0

    def create_regions(self):
        # "Menu" is the required starting point.
        menu_region = Region("Menu", self.player, self.multiworld)
        self.multiworld.regions.append(menu_region)

        # "The Great Sea" region contains all locations not in a randomizable region.
        great_sea_region = Region("The Great Sea", self.player, self.multiworld)
        self.multiworld.regions.append(great_sea_region)

        # Add all randomizable regions.
        for region in ALL_ENTRANCES + ALL_EXITS:
            self.multiworld.regions.append(Region(region, self.player, self.multiworld))

        # Assign each location to their region.
        for location, data in LOCATION_TABLE.items():
            region = self.multiworld.get_region(data.region, self.player)
            region.locations.append(TWWLocation(self.player, location, region, data))

        # Connect the "Menu" region to the "The Great Sea" region.
        menu_region.connect(great_sea_region)

        # Connect the dungeon, secret caves, and fairy fountain regions to the "The Great Sea" region.
        for entrance in DUNGEON_ENTRANCES + SECRET_CAVES_ENTRANCES + FAIRY_FOUNTAIN_ENTRANCES:
            rule = lambda state, entrance=entrance: getattr(Macros, self._get_access_rule(entrance))(state, self.player)
            connecting_region = self.multiworld.get_region(entrance, self.player)
            great_sea_region.connect(connecting_region, rule=rule)

        # Connect nested regions with their parent region
        for entrance in MINIBOSS_ENTRANCES + BOSS_ENTRANCES + SECRET_CAVES_INNER_ENTRANCES:
            parent_region_name = entrance.split(" in ")[-1]
            # Consider Hyrule Castle and Forsaken Fortress as part of The Great Sea (regions are not randomizable).
            if parent_region_name in ["Hyrule Castle", "Forsaken Fortress"]:
                parent_region_name = "The Great Sea"
            rule = lambda state, entrance=entrance: getattr(Macros, self._get_access_rule(entrance))(state, self.player)
            parent_region = self.multiworld.get_region(parent_region_name, self.player)
            connecting_region = self.multiworld.get_region(entrance, self.player)
            parent_region.connect(connecting_region, rule=rule)

    def create_item(self, item: str) -> TWWItem:
        # TODO: calculate nonprogress items dynamically
        set_non_progress = False
        if not self.options.progression_dungeons and item.endswith(" Key"):
            set_non_progress = True
        if not self.options.progression_triforce_charts and item.startswith("Triforce Chart"):
            set_non_progress = True
        if not self.options.progression_treasure_charts and item.startswith("Treasure Chart"):
            set_non_progress = True

        if item in ITEM_TABLE:
            return TWWItem(item, self.player, ITEM_TABLE[item], set_non_progress)
        raise Exception(f"Invalid item name: {item}")

    def pre_fill(self):
        # Ban the Bait Bag slot from having bait.
        beedle_20 = self.multiworld.get_location("The Great Sea - Beedle's Shop Ship - 20 Rupee Item", self.player)
        add_item_rule(beedle_20, lambda item: item.name not in ["All-Purpose Bait", "Hyoi Pear"])

        # Also ban the same item from appearing more than once in the Rock Spire Isle shop ship.
        beedle_500 = self.multiworld.get_location(
            "Rock Spire Isle - Beedle's Special Shop Ship - 500 Rupee Item", self.player
        )
        beedle_950 = self.multiworld.get_location(
            "Rock Spire Isle - Beedle's Special Shop Ship - 950 Rupee Item", self.player
        )
        beedle_900 = self.multiworld.get_location(
            "Rock Spire Isle - Beedle's Special Shop Ship - 900 Rupee Item", self.player
        )
        add_item_rule(
            beedle_500,
            lambda item, locs=[beedle_950, beedle_900]: (
                (item.game == "The Wind Waker" and all(l.item is None or item.name != l.item.name for l in locs))
                or (
                    item.game != "The Wind Waker"
                    and all(l.item is None or l.item.game == "The Wind Waker" for l in locs)
                )
            ),
        )
        add_item_rule(
            beedle_950,
            lambda item, locs=[beedle_500, beedle_900]: (
                (item.game == "The Wind Waker" and all(l.item is None or item.name != l.item.name for l in locs))
                or (
                    item.game != "The Wind Waker"
                    and all(l.item is None or l.item.game == "The Wind Waker" for l in locs)
                )
            ),
        )
        add_item_rule(
            beedle_900,
            lambda item, locs=[beedle_500, beedle_950]: (
                (item.game == "The Wind Waker" and all(l.item is None or item.name != l.item.name for l in locs))
                or (
                    item.game != "The Wind Waker"
                    and all(l.item is None or l.item.game == "The Wind Waker" for l in locs)
                )
            ),
        )

        # Randomize which chart points to each sector, if the option is enabled.
        if self.options.randomize_charts:
            self._randomize_charts()

        # Set nonprogress location from options.
        self._set_nonprogress_locations()

        # Select required bosses.
        if self.options.required_bosses:
            self._randomize_required_bosses()

        # Randomize entrances to exits, if the option is set.
        entrance_exit_pairs = self._randomize_entrances()

        # Connect entrances to exits.
        for entrance_region, exit_region in entrance_exit_pairs:
            rule = lambda state, entrance=entrance_region.name: getattr(Macros, self._get_access_rule(entrance))(
                state, self.player
            )
            entrance_region.connect(exit_region, rule=rule)

    @classmethod
    def stage_pre_fill(cls, multiworld: MultiWorld):
        # Reference: `fill_dungeons_restrictive()` from ALTTP
        dungeon_shortnames: dict[str, str] = {
            "Dragon Roost Cavern": "DRC",
            "Forbidden Woods": "FW",
            "Tower of the Gods": "TotG",
            "Forsaken Fortress": "FF",
            "Earth Temple": "ET",
            "Wind Temple": "WT",
        }

        in_dungeon_items: list[TWWItem] = []
        own_dungeon_items: set[tuple[int, str]] = set()
        for subworld in multiworld.get_game_worlds("The Wind Waker"):
            player = subworld.player
            if player not in multiworld.groups:
                in_dungeon_items += [item for item in subworld.pre_fill_items]
                own_dungeon_items |= {(player, item_name) for item_name in subworld.own_dungeon_item_names}

        if in_dungeon_items:
            locations: list[TWWLocation] = [
                location
                for world in multiworld.get_game_worlds("The Wind Waker")
                for location in world._get_dungeon_locations()
                if not location.item
            ]

            if own_dungeon_items:
                for location in locations:
                    dungeon = location.name.split(" - ")[0]
                    orig_rule = location.item_rule
                    location.item_rule = lambda item, dungeon=dungeon, orig_rule=orig_rule: (
                        not (item.player, item.name) in own_dungeon_items
                        or item.name.startswith(dungeon_shortnames[dungeon])
                    ) and orig_rule(item)

            multiworld.random.shuffle(locations)
            # Dungeon-locked items have to be placed first, to not run out of spaces for dungeon-locked items.
            # Subsort in the order Big Key, Small Key, Other before placing dungeon items.

            sort_order = {"BKey": 3, "SKey": 2}
            in_dungeon_items.sort(
                key=lambda item: sort_order.get(item.type, 1)
                + (5 if (item.player, item.name) in own_dungeon_items else 0)
            )

            # Construct a partial `all_state` which contains only the items from `get_pre_fill_items`, which aren't
            # in_dungeon.
            in_dungeon_player_ids = {item.player for item in in_dungeon_items}
            all_state_base = CollectionState(multiworld)
            for item in multiworld.itempool:
                multiworld.worlds[item.player].collect(all_state_base, item)
            pre_fill_items = []
            for player in in_dungeon_player_ids:
                pre_fill_items += multiworld.worlds[player].get_pre_fill_items()
            for item in in_dungeon_items:
                try:
                    pre_fill_items.remove(item)
                except ValueError:
                    # `pre_fill_items` should be a subset of `in_dungeon_items`, but just in case.
                    pass
            for item in pre_fill_items:
                multiworld.worlds[item.player].collect(all_state_base, item)
            all_state_base.sweep_for_events()

            # Remove completion condition so that minimal-accessibility worlds place keys properly.
            for player in {item.player for item in in_dungeon_items}:
                if all_state_base.has("Victory", player):
                    all_state_base.remove(multiworld.worlds[player].create_item("Victory"))

            fill_restrictive(
                multiworld,
                all_state_base,
                locations,
                in_dungeon_items,
                single_player_placement=True,
                lock=True,
                allow_excluded=True,
                name="TWW Dungeon Items",
            )

    def create_items(self):
        exclude = [item.name for item in self.multiworld.precollected_items[self.player]]
        for item, data in ITEM_TABLE.items():
            if item == "Victory":
                # Victory item is always on Defeat Ganondorf location.
                self.multiworld.get_location("Defeat Ganondorf", self.player).place_locked_item(self.create_item(item))
            elif item in self.vanilla_dungeon_item_names:
                # Place desired vanilla dungeon item in their vanilla locations.
                for location in VANILLA_DUNGEON_ITEM_LOCATIONS[item]:
                    self.multiworld.get_location(location, self.player).place_locked_item(self.create_item(item))
            else:
                if item == "Progressive Sword" and self.options.sword_mode == "swordless":
                    continue

                copies_to_place = data.quantity - exclude.count(item)
                for _ in range(copies_to_place):
                    if item in self.own_dungeon_item_names or item in self.any_dungeon_item_names:
                        self.pre_fill_items.append(self.create_item(item))
                    else:
                        self.itempool.append(self.create_item(item))

        # Calculate the number of additional filler items to create to fill all locations.
        n_locations = len(self.multiworld.get_unfilled_locations(self.player))
        n_items = len(self.pre_fill_items) + len(self.itempool)
        n_filler_items = n_locations - n_items

        # Add filler items to the item pool.
        for _ in range(n_filler_items):
            self.itempool.append(self.create_item(self.get_filler_item_name()))

        self.multiworld.itempool += self.itempool

    def get_filler_item_name(self) -> str:
        # Use the same weights for filler items that are used in the base rando.
        filler_consumables = [
            "Yellow Rupee",
            "Red Rupee",
            "Purple Rupee",
            "Orange Rupee",
            "Joy Pendant",
        ]
        filler_weights = [3, 7, 10, 15, 3]
        return self.multiworld.random.choices(filler_consumables, weights=filler_weights, k=1)[0]

    def set_rules(self):
        set_rules(self.multiworld, self.player)

    def generate_output(self, output_directory: str):
        # Output seed name and slot number to seed RNG in randomizer client.
        output_data = {
            "Seed": self.multiworld.seed_name,
            "Slot": self.player,
            "Name": self.multiworld.get_player_name(self.player),
            "Options": {},
            "Required Bosses": self.required_boss_item_locations,
            "Locations": {},
            "Entrances": {},
            "Charts": self.island_number_to_chart_name,
        }

        # Output relevant options to file.
        for field in fields(self.options):
            output_data["Options"][field.name] = getattr(self.options, field.name).value

        # Output which item has been placed at each location.
        locations = self.multiworld.get_locations(self.player)
        for location in locations:
            if location.name != "Defeat Ganondorf":
                if location.item:
                    item_info = {
                        "player": location.item.player,
                        "name": location.item.name,
                        "game": location.item.game,
                        "classification": location.item.classification.name,
                    }
                else:
                    item_info = {
                        "name": "Nothing",
                        "game": "The Wind Waker",
                        "classification": "filler",
                    }
                output_data["Locations"][location.name] = item_info

        # Output the mapping of entrances to exits.
        entrances = self.multiworld.get_entrances(self.player)
        for entrance in entrances:
            if entrance.parent_region.name in ALL_ENTRANCES:
                output_data["Entrances"][entrance.parent_region.name] = entrance.connected_region.name

        # Output the plando details to file.
        file_path = os.path.join(output_directory, f"{self.multiworld.get_out_file_name_base(self.player)}.aptww")
        with open(file_path, "w") as f:
            f.write(yaml.dump(output_data, sort_keys=False))

    def fill_slot_data(self):
        return {
            "progression_dungeons": self.options.progression_dungeons.value,
            "progression_tingle_chests": self.options.progression_tingle_chests.value,
            "progression_dungeon_secrets": self.options.progression_dungeon_secrets.value,
            "progression_puzzle_secret_caves": self.options.progression_puzzle_secret_caves.value,
            "progression_combat_secret_caves": self.options.progression_combat_secret_caves.value,
            "progression_savage_labyrinth": self.options.progression_savage_labyrinth.value,
            "progression_great_fairies": self.options.progression_great_fairies.value,
            "progression_short_sidequests": self.options.progression_short_sidequests.value,
            "progression_long_sidequests": self.options.progression_long_sidequests.value,
            "progression_spoils_trading": self.options.progression_spoils_trading.value,
            "progression_minigames": self.options.progression_minigames.value,
            "progression_battlesquid": self.options.progression_battlesquid.value,
            "progression_free_gifts": self.options.progression_free_gifts.value,
            "progression_mail": self.options.progression_mail.value,
            "progression_platforms_rafts": self.options.progression_platforms_rafts.value,
            "progression_submarines": self.options.progression_submarines.value,
            "progression_eye_reef_chests": self.options.progression_eye_reef_chests.value,
            "progression_big_octos_gunboats": self.options.progression_big_octos_gunboats.value,
            "progression_triforce_charts": self.options.progression_triforce_charts.value,
            "progression_treasure_charts": self.options.progression_treasure_charts.value,
            "progression_expensive_purchases": self.options.progression_expensive_purchases.value,
            "progression_island_puzzles": self.options.progression_island_puzzles.value,
            "progression_misc": self.options.progression_misc.value,
            "randomize_mapcompass": self.options.randomize_mapcompass.value,
            "randomize_smallkeys": self.options.randomize_smallkeys.value,
            "randomize_bigkeys": self.options.randomize_bigkeys.value,
            "sword_mode": self.options.sword_mode.value,
            "required_bosses": self.options.required_bosses.value,
            "num_required_bosses": self.options.num_required_bosses.value,
            "chest_type_matches_contents": self.options.chest_type_matches_contents.value,
            "included_dungeons": self.options.included_dungeons.value,
            "excluded_dungeons": self.options.excluded_dungeons.value,
            # "trap_chests": self.options.trap_chests.value,
            "hero_mode": self.options.hero_mode.value,
            "logic_obscurity": self.options.logic_obscurity.value,
            "logic_precision": self.options.logic_precision.value,
            "enable_tuner_logic": self.options.enable_tuner_logic.value,
            "randomize_dungeon_entrances": self.options.randomize_dungeon_entrances.value,
            "randomize_secret_cave_entrances": self.options.randomize_secret_cave_entrances.value,
            "randomize_miniboss_entrances": self.options.randomize_miniboss_entrances.value,
            "randomize_boss_entrances": self.options.randomize_boss_entrances.value,
            "randomize_secret_cave_inner_entrances": self.options.randomize_secret_cave_inner_entrances.value,
            "randomize_fairy_fountain_entrances": self.options.randomize_fairy_fountain_entrances.value,
            "mix_entrances": self.options.mix_entrances.value,
            "randomize_enemies": self.options.randomize_enemies.value,
            # "randomize_music": self.options.randomize_music.value,
            "randomize_starting_island": self.options.randomize_starting_island.value,
            "randomize_charts": self.options.randomize_charts.value,
            # "hoho_hints": self.options.hoho_hints.value,
            # "fishmen_hints": self.options.fishmen_hints.value,
            # "korl_hints": self.options.korl_hints.value,
            # "num_item_hints": self.options.num_item_hints.value,
            # "num_location_hints": self.options.num_location_hints.value,
            # "num_barren_hints": self.options.num_barren_hints.value,
            # "num_path_hints": self.options.num_path_hints.value,
            # "prioritize_remote_hints": self.options.prioritize_remote_hints.value,
            "swift_sail": self.options.swift_sail.value,
            "instant_text_boxes": self.options.instant_text_boxes.value,
            "reveal_full_sea_chart": self.options.reveal_full_sea_chart.value,
            "add_shortcut_warps_between_dungeons": self.options.add_shortcut_warps_between_dungeons.value,
            "skip_rematch_bosses": self.options.skip_rematch_bosses.value,
            "remove_music": self.options.remove_music.value,
            "death_link": self.options.death_link.value,
        }
