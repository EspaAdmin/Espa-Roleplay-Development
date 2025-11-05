import discord
from discord import app_commands
from discord.ext import commands
import json
import sqlite3
from typing import Any, cast
from difflib import get_close_matches

from espa8_bot import DB_PATH
import treaty_utils
from treaty_utils import TreatyClauses, TreatyConditions

## Treaty Args JSON format
# {"clauses": [{"clauseEnum": TreatyClauses.value,
#               "clauseArgs": {"custom key": custom_value, ...},
#                "conditions": {"condEnum": TreatyConditions.value, "condArgs": {"custom key": custom_value, ...}}
#              }, ...],
#  "entryIntoForce": [{"blockType": "auto", "conditions": [{"condEnum": TreatyConditions.value, "condArgs": {"custom key": custom_value, ...}}, ...]}, ...],
#  "suspension":     [{"blockType": "vote", "voteArgs": {"custom key": custom_value, ...}, "conditions": ["custom string", ...]}, ...],
#  "termination":    [{"blockType": "auto", "conditions": [{"condEnum": TreatyConditions.value, "condArgs": {"custom key": custom_value, ...}}, ...]},
#                     {"blockType": "vote", "voteArgs": {"custom key": custom_value, ...}, "conditions": ["custom string", ...]}, ...
#                    ],
#  "participation":  [{"blockType": "auto", "conditions": [{"condEnum": TreatyConditions.value, "condArgs": {"custom key": custom_value, ...}}, ...]},
#                     {"blockType": "vote", "voteArgs": {"custom key": custom_value, ...}, "conditions": ["custom string", ...]}, ...
#                    ],
#  "withdrawal":     [{"blockType": "auto", "conditions": [{"condEnum": TreatyConditions.value, "condArgs": {"custom key": custom_value, ...}}, ...]},
#                     {"blockType": "vote", "voteArgs": {"custom key": custom_value, ...}, "conditions": ["custom string", ...]}, ...
#                    ],
#  "expulsion":      [{"blockType": "vote", "voteArgs": {"custom key": custom_value, ...}, "conditions": ["custom string", ...]}, ...],
#  "inForceDate": "01/01/1954"
# }
# 
## Stages of Treaty: ["Draft", "Final", "InForce", "Terminated", "Suspended"]
#
## signed_treaties Column JSON Format
# [{"treatyID": 12, "dateSigned": "01/01/1952"}, ...]

class Treaties(commands.Cog):
    def __init__(self, bot : commands.Bot):
        self.bot = bot
    
    
    ## Draft Treaty (Player): Creates a new treaty
    @app_commands.command(name = "draft_treaty", description = "Draft a new treaty")
    @app_commands.describe(
        name = "Name of new treaty"
    )
    # @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def draft_treaty(
        self,
        interaction: discord.Interaction,
        name: str
    ):
        with sqlite3.connect(DB_PATH) as conn:
            cursor: sqlite3.Cursor = conn.cursor() 

            playerID = interaction.user.id
            if len(name) > 100: raise ValueError("name of treaty must be max 100 characters")

            countryID = treaty_utils.GetCountryIDFromUser(cursor = cursor, userID = playerID)
            
            ## TEMP CODE REMOVE LATER
            cursor.execute("DELETE FROM treaties")
            
            cursor.execute("""
                INSERT INTO treaties (treaty_name, drafter_country_id, treaty_args, treaty_status)
                VALUES (?, ?, ?, ?)
            """, (name, countryID, "{\"clauses\": []}", "Draft"))

            treatyID = cursor.lastrowid
            if treatyID is None: raise ValueError("treatyID is None")

            editTreatyMessage = EditTreatyMessage(cursor=cursor, userID=interaction.user.id, treatyID=treatyID)
            await interaction.response.send_message(embed = editTreatyMessage.embed, view = editTreatyMessage.view)
            editTreatyMessage.view.message = await interaction.original_response()

            conn.commit()

    ## Edit Treaty (Player): Edits a treaty
    @app_commands.command(name = "edit_treaty", description = "Edit a treaty")
    @app_commands.describe(
        name = "Name of treaty"
    )
    # @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def edit_treaty(
        self,
        interaction: discord.Interaction,
        name: str
    ):
        with sqlite3.connect(DB_PATH) as conn:
            cursor: sqlite3.Cursor = conn.cursor()

            playerID = interaction.user.id

            cursor.execute("""
                SELECT treaty_id, treaty_name, drafter_country_id, treaty_status
                FROM treaties
            """)
            treatyRows = cursor.fetchall()
            if not treatyRows: raise ValueError("no existing treaty found")
            treatyNameList = [treatyRow[1] for treatyRow in treatyRows]
            close_matches = get_close_matches(name, treatyNameList, n = 1)
            if not close_matches: raise ValueError(f"treaty {name} not found")
            treatyRow = treatyRows[treatyNameList.index(close_matches[0])]
            if treatyRow[3] != "Draft": raise ValueError(f"treaty is not in draft, cannot edit")

            countryID = treaty_utils.GetCountryIDFromUser(cursor = cursor, userID = playerID)

            if treatyRow[2] != countryID: raise ValueError("only the author country of a treaty may edit it")
            treatyID = treatyRow[0]

            editTreatyMessage = EditTreatyMessage(cursor=cursor, userID = interaction.user.id, treatyID = treatyID)
            await interaction.response.send_message(content = None, embed = editTreatyMessage.embed, view = editTreatyMessage.view)
            editTreatyMessage.view.message = await interaction.original_response()

            conn.commit()
    
    ## View Treaty (Player): View a treaty
    @app_commands.command(name = "view_treaty", description = "View a treaty")
    @app_commands.describe(
        name = "Name of treaty"
    )
    # @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def view_treaty(
        self,
        interaction: discord.Interaction,
        name: str
    ):
        with sqlite3.connect(DB_PATH) as conn:
            cursor: sqlite3.Cursor = conn.cursor()

            cursor.execute("""
                SELECT treaty_id, treaty_name
                FROM treaties
            """)
            treatyRows = cursor.fetchall()
            if not treatyRows: raise ValueError("no existing treaty found")
            treatyNameList = [treatyRow[1] for treatyRow in treatyRows]
            close_matches = get_close_matches(name, treatyNameList, n = 1)
            if not close_matches: raise ValueError(f"treaty {name} not found")
            treatyRow = treatyRows[treatyNameList.index(close_matches[0])]
            treatyID = treatyRow[0]

            viewTreatyMessage = ViewTreatyMessage(cursor=cursor, userID = interaction.user.id, treatyID = treatyID)
            await interaction.response.send_message(content = None, embed = viewTreatyMessage.embed, view = viewTreatyMessage.view)
            viewTreatyMessage.view.message = await interaction.original_response()

            conn.commit()

class ViewTreatyMessage:

    def __init__(self, cursor: sqlite3.Cursor, userID: int, treatyID: int, timeout = 600):
        self.timeout = timeout

        treatyInfo = treaty_utils.GetAllTreatyStrings(cursor = cursor, treatyID = treatyID)
        treatyName = treatyInfo["name"]
        treatyFullString = treatyInfo["fullText"]
        treatyStatus = treatyInfo["status"]
        description = treatyFullString + f"\nStatus:\n" + treatyStatus
        self.embed = discord.Embed(
            title = f"{treatyName}",
            description = description
        )

        self.view = self.ViewTreatyView(cursor=cursor, userID=userID, treatyID=treatyID, timeout=timeout)

    class ViewTreatyView(discord.ui.View):
        def __init__(self, cursor: sqlite3.Cursor, userID: int, treatyID: int, timeout):

            super().__init__(timeout=timeout)
            self.message: discord.Message | None = None
            self.userID = userID
            self.treatyID = treatyID

            # Check what buttons the user should get:
            # . if they haven't signed and it's final or suspended, then 'Sign'
            # . if they haven't signed and it's in force, then 'Ratify'
            # . if they've signed, then 'Withdraw'
            signatoryList = treaty_utils.GetAllSignatories(cursor = cursor, treatyID = treatyID)
            countryID = treaty_utils.GetCountryIDFromUser(cursor = cursor, userID = userID)
            signAllowed = treaty_utils.CheckAutoCondBlocks(cursor = cursor, condType = "participation", treatyID = treatyID, countryID = countryID)
            withdrawAllowed = treaty_utils.CheckAutoCondBlocks(cursor = cursor, condType = "withdrawal", treatyID = treatyID, countryID = countryID)

            cursor.execute("""
                SELECT treaty_status 
                FROM treaties 
                WHERE treaty_id == (?)
            """, (treatyID,))
            treatyStatus = cursor.fetchone()
            if treatyStatus is None: raise ValueError(f"treaty status is null for treaty with id {treatyID}")
            treatyStatus = treatyStatus[0]
            
            if treatyStatus in ["Final", "Suspended"] and countryID not in signatoryList and signAllowed:
                signButton: discord.ui.Button = discord.ui.Button(
                    style = discord.ButtonStyle.blurple,
                    label = "Sign",
                    row = 0)
                signButton.callback = self.sign_button_callback # type: ignore
                self.add_item(signButton)
            if treatyStatus == "InForce" and countryID not in signatoryList and signAllowed:
                ratifyButton: discord.ui.Button = discord.ui.Button(
                    style = discord.ButtonStyle.blurple,
                    label = "Ratify",
                    row = 0)
                ratifyButton.callback = self.ratify_button_callback # type: ignore
                self.add_item(ratifyButton)
            if countryID in signatoryList and withdrawAllowed:
                withdrawButton: discord.ui.Button = discord.ui.Button(
                    style = discord.ButtonStyle.blurple,
                    label = "Withdraw",
                    row = 0)
                withdrawButton.callback = self.withdraw_button_callback # type: ignore
                self.add_item(withdrawButton)
        
        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if self.userID is None or interaction.user.id == self.userID: return await super().interaction_check(interaction)
            else: return False
        
        async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
            import traceback
            traceback.print_exception(type(error), error, error.__traceback__)  # full traceback in console

            # respond to the user if possible
            if interaction.response.is_done():
                await interaction.followup.send(f"An error occured: {str(error)}", ephemeral=True)
            else:
                await interaction.response.send_message(f"An error occured: {str(error)}", ephemeral=True)
        
        async def on_timeout(self):
            if self.message or getattr(self.message, "view", None) is self:
                # disable all components
                for child in self.children:
                    child.disabled = True
                await self.message.edit(
                    content="Interaction Timed Out",
                    view=self
                )
        
        async def sign_button_callback(self, interaction: discord.Interaction):
            with sqlite3.connect(DB_PATH) as conn:
                cursor: sqlite3.Cursor = conn.cursor()

                countryID = treaty_utils.GetCountryIDFromUser(cursor = cursor, userID = self.userID)
                treaty_utils.AddSignatoryToDB(cursor = cursor, treatyID = self.treatyID, countryID = countryID)

                if treaty_utils.CheckAutoCondBlocks(cursor = cursor, treatyID = self.treatyID, condType = "entryIntoForce", countryID = countryID):
                    treaty_utils.EnterTreatyIntoForce(cursor = cursor, treatyID = self.treatyID)

                viewTreatyMessage = ViewTreatyMessage(cursor = cursor, userID = self.userID, treatyID = self.treatyID)
                await interaction.response.send_message(content = None, embed = viewTreatyMessage.embed, view = viewTreatyMessage.view)
                viewTreatyMessage.view.message = await interaction.original_response()

                conn.commit()

        async def ratify_button_callback(self, interaction: discord.Interaction):
            with sqlite3.connect(DB_PATH) as conn:
                cursor: sqlite3.Cursor = conn.cursor()

                countryID = treaty_utils.GetCountryIDFromUser(cursor = cursor, userID = self.userID)
                treaty_utils.AddSignatoryToDB(cursor = cursor, treatyID = self.treatyID, countryID = countryID)

                viewTreatyMessage = ViewTreatyMessage(cursor = cursor, userID = self.userID, treatyID = self.treatyID)
                await interaction.response.send_message(content = None, embed = viewTreatyMessage.embed, view = viewTreatyMessage.view)
                viewTreatyMessage.view.message = await interaction.original_response()

                conn.commit()

        async def withdraw_button_callback(self, interaction: discord.Interaction):
            with sqlite3.connect(DB_PATH) as conn:
                cursor: sqlite3.Cursor = conn.cursor()

                countryID = treaty_utils.GetCountryIDFromUser(cursor = cursor, userID = self.userID)
                treaty_utils.DeleteSignatoryFromDB(cursor = cursor, treatyID = self.treatyID, countryID = countryID)

                viewTreatyMessage = ViewTreatyMessage(cursor = cursor, userID = self.userID, treatyID = self.treatyID)
                await interaction.response.send_message(content = None, embed = viewTreatyMessage.embed, view = viewTreatyMessage.view)
                viewTreatyMessage.view.message = await interaction.original_response()

                conn.commit()

class EditClausesMessage:

    def __init__(self, cursor: sqlite3.Cursor, userID: int, treatyID: int, timeout = 600):
        
        self.timeout = timeout

        treatyInfo = treaty_utils.GetAllTreatyStrings(cursor = cursor, treatyID = treatyID)
        treatyName = treatyInfo["name"]
        treatyClausesString = treatyInfo["clauses"]
        self.embed = discord.Embed(
            title = f"Editing '{treatyName}' Clauses",
            description = treatyClausesString
        )

        self.view = self.EditClausesView(userID=userID, treatyID=treatyID, timeout=timeout)

    class EditClausesView(discord.ui.View):
        def __init__(self, userID: int, treatyID: int, timeout):
            super().__init__(timeout=timeout)
            self.message: discord.Message | None = None
            self.userID = userID
            self.treatyID = treatyID
        
        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if self.userID is None or interaction.user.id == self.userID: return await super().interaction_check(interaction)
            else: return False
        
        async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
            import traceback
            traceback.print_exception(type(error), error, error.__traceback__)  # full traceback in console

            # respond to the user if possible
            if interaction.response.is_done():
                await interaction.followup.send(f"An error occured: {str(error)}", ephemeral=True)
            else:
                await interaction.response.send_message(f"An error occured: {str(error)}", ephemeral=True)
        
        async def on_timeout(self):
            if self.message or getattr(self.message, "view", None) is self:
                # disable all components
                for child in self.children:
                    child.disabled = True
                await self.message.edit(
                    content="Interaction Timed Out",
                    view=self
                )
        
        @discord.ui.button(label = "Add Clause", style = discord.ButtonStyle.green, row = 0)
        async def add_clause_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
            with sqlite3.connect(DB_PATH) as conn:
                cursor: sqlite3.Cursor = conn.cursor()

                selectClauseCategoryMessage = SelectClauseCategoryMessage(cursor=cursor, userID=self.userID, treatyID=self.treatyID)
                await interaction.response.edit_message(content = None, embed = selectClauseCategoryMessage.embed, view = selectClauseCategoryMessage.view)
                selectClauseCategoryMessage.view.message = await interaction.original_response()

                conn.commit()
        
        @discord.ui.button(label = "Edit Clause", style = discord.ButtonStyle.blurple, row = 0)
        async def edit_clause_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
            selectClauseModal = SelectClauseModal(userID = self.userID, treatyID = self.treatyID)
            await interaction.response.send_modal(selectClauseModal)
        
        @discord.ui.button(label = "Delete Clause", style = discord.ButtonStyle.red, row = 0)
        async def delete_clause_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
            addTreatyClauseModal = DeleteClauseModal(userID=self.userID, treatyID=self.treatyID)
            await interaction.response.send_modal(addTreatyClauseModal)
        
        @discord.ui.button(label = "Edit Treaty", style = discord.ButtonStyle.blurple, row = 1)
        async def edit_treaty_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
            with sqlite3.connect(DB_PATH) as conn:
                cursor: sqlite3.Cursor = conn.cursor()

                editTreatyMessage = EditTreatyMessage(cursor=cursor, userID=self.userID, treatyID=self.treatyID)
                await interaction.response.edit_message(content = None, embed = editTreatyMessage.embed, view = editTreatyMessage.view)
                editTreatyMessage.view.message = await interaction.original_response()

                conn.commit()

class EditConditionBlocksMessage:

    def __init__(self, cursor: sqlite3.Cursor, userID: int, treatyID: int, condType: str, timeout = 600):
        
        self.timeout = timeout

        treatyInfo = treaty_utils.GetAllTreatyStrings(cursor = cursor, treatyID = treatyID)
        treatyName = treatyInfo["name"]
        if condType == "clause":
            descString = treatyInfo["clauses"]
        else:
            descString = treatyInfo[condType]
        
        self.embed = discord.Embed(
            title = f"Editing '{treatyName}' Condition Blocks",
            description = descString
        )

        self.view = self.EditConditionBlocksView(userID = userID, treatyID = treatyID, condType = condType, timeout = timeout)

    class EditConditionBlocksView(discord.ui.View):
        def __init__(self, userID: int, treatyID: int, condType: str, timeout):
            super().__init__(timeout=timeout)
            self.message: discord.Message | None = None
            self.userID = userID
            self.treatyID = treatyID
            self.condType = condType

            autoCondTypes = ["entryIntoForce", "termination", "participation", "withdrawal"]
            voteCondTypes = ["termination", "suspension", "participation", "withdrawal", "expulsion"]

            if condType in autoCondTypes:
                autoCondButton: discord.ui.Button = discord.ui.Button(
                    style = discord.ButtonStyle.green,
                    label = "Add Auto Condition Block",
                    row = 0
                )
                autoCondButton.callback = self.add_auto_condition_block_button_callback # type: ignore
                self.add_item(autoCondButton)
            if condType in voteCondTypes:
                voteCondButton: discord.ui.Button = discord.ui.Button(
                    style = discord.ButtonStyle.green,
                    label = "Add Vote Condition Block",
                    row = 0
                )
                voteCondButton.callback = self.add_vote_condition_block_button_callback # type: ignore
                self.add_item(voteCondButton)
        
        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if self.userID is None or interaction.user.id == self.userID: return await super().interaction_check(interaction)
            else: return False
        
        async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
            import traceback
            traceback.print_exception(type(error), error, error.__traceback__)  # full traceback in console

            # respond to the user if possible
            if interaction.response.is_done():
                await interaction.followup.send(f"An error occured: {str(error)}", ephemeral=True)
            else:
                await interaction.response.send_message(f"An error occured: {str(error)}", ephemeral=True)
        
        async def on_timeout(self):
            if self.message or getattr(self.message, "view", None) is self:
                # disable all components
                for child in self.children:
                    child.disabled = True
                await self.message.edit(
                    content="Interaction Timed Out",
                    view=self
                )
        
        async def add_auto_condition_block_button_callback(self, interaction: discord.Interaction):
            with sqlite3.connect(DB_PATH) as conn:
                cursor: sqlite3.Cursor = conn.cursor()

                blockIndex = treaty_utils.AddAutoConditionBlockToDB(cursor = cursor, treatyID = self.treatyID, condType = self.condType)

                editAutoConditionBlockMessage = EditAutoConditionBlockMessage(cursor = cursor, userID = self.userID, treatyID = self.treatyID, condType = self.condType, blockIndex = blockIndex)
                await interaction.response.edit_message(content = None, embed = editAutoConditionBlockMessage.embed, view = editAutoConditionBlockMessage.view)
                editAutoConditionBlockMessage.view.message = await interaction.original_response()

                conn.commit()
        
        async def add_vote_condition_block_button_callback(self, interaction: discord.Interaction):
            addVoteConditionBlockModal = AddVoteConditionBlockModal(userID = self.userID, treatyID = self.treatyID, condType = self.condType)
            await interaction.response.send_modal(addVoteConditionBlockModal)
        
        @discord.ui.button(label = "Delete Condition Block", style = discord.ButtonStyle.red, row = 1)
        async def delete_condition_block_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
            deleteConditionBlockModal = DeleteConditionBlockModal(userID = self.userID, treatyID = self.treatyID, condType = self.condType)
            await interaction.response.send_modal(deleteConditionBlockModal)
        
        @discord.ui.button(label = "Edit Condition Block", style = discord.ButtonStyle.blurple, row = 1)
        async def edit_condition_block_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
            selectConditionBlockModal = SelectConditionBlockModal(userID = self.userID, treatyID = self.treatyID, condType = self.condType)
            await interaction.response.send_modal(selectConditionBlockModal)
        
        @discord.ui.button(label = "Edit Treaty", style = discord.ButtonStyle.blurple, row = 2)
        async def edit_treaty_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
            with sqlite3.connect(DB_PATH) as conn:
                cursor: sqlite3.Cursor = conn.cursor()

                editTreatyMessage = EditTreatyMessage(cursor=cursor, userID=self.userID, treatyID=self.treatyID)
                await interaction.response.edit_message(content = None, embed = editTreatyMessage.embed, view = editTreatyMessage.view)
                editTreatyMessage.view.message = await interaction.original_response()

                conn.commit()

class DeleteClauseModal(discord.ui.Modal):

    def __init__(self, userID: int, treatyID: int):
        self.userID = userID
        self.treatyID = treatyID
        
        title = "Delete Treaty Clause"
        super().__init__(title=title)

        self.textInput: discord.ui.TextInput = discord.ui.TextInput(
            label = "Choose the clause no to delete",
            style = discord.TextStyle.short,
            placeholder = "Enter an integer...",
            required = True
        )
        self.add_item(self.textInput)
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.userID is None or interaction.user.id == self.userID: return await super().interaction_check(interaction)
        else: return False
    
    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None: # type: ignore
            import traceback
            traceback.print_exception(type(error), error, error.__traceback__)  # full traceback in console

            # respond to the user if possible
            if interaction.response.is_done():
                await interaction.followup.send(f"An error occured: {str(error)}", ephemeral=True)
            else:
                await interaction.response.send_message(f"An error occured: {str(error)}", ephemeral=True)
    
    async def on_submit(self, interaction: discord.Interaction):
        with sqlite3.connect(DB_PATH) as conn:
            cursor: sqlite3.Cursor = conn.cursor()

            if not self.textInput.value.isdigit(): raise ValueError("value entered is not positive integer")
            clauseIndex = int(self.textInput.value) - 1
            treaty_utils.DeleteClauseFromDB(cursor = cursor, treatyID = self.treatyID, clauseIndex = clauseIndex)
            
            editClausesMessage = EditClausesMessage(cursor=cursor, userID = self.userID, treatyID = self.treatyID)
            await interaction.response.edit_message(content = None, embed = editClausesMessage.embed, view = editClausesMessage.view)
            editClausesMessage.view.message = await interaction.original_response()

            conn.commit()

class DeleteConditionBlockModal(discord.ui.Modal):

    def __init__(self, userID: int, treatyID: int, condType: str):
        self.userID = userID
        self.treatyID = treatyID
        self.condType = condType
        
        title = "Delete Condition Block"
        super().__init__(title=title)

        self.textInput: discord.ui.TextInput = discord.ui.TextInput(
            label = "Choose the block no to delete",
            style = discord.TextStyle.short,
            placeholder = "Enter an integer...",
            required = True
        )
        self.add_item(self.textInput)
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.userID is None or interaction.user.id == self.userID: return await super().interaction_check(interaction)
        else: return False
    
    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None: # type: ignore
            import traceback
            traceback.print_exception(type(error), error, error.__traceback__)  # full traceback in console

            # respond to the user if possible
            if interaction.response.is_done():
                await interaction.followup.send(f"An error occured: {str(error)}", ephemeral=True)
            else:
                await interaction.response.send_message(f"An error occured: {str(error)}", ephemeral=True)
    
    async def on_submit(self, interaction: discord.Interaction):
        with sqlite3.connect(DB_PATH) as conn:
            cursor: sqlite3.Cursor = conn.cursor()

            if not self.textInput.value.isdigit(): raise ValueError("value entered is not positive integer")
            blockIndex = int(self.textInput.value) - 1
            treaty_utils.DeleteConditionBlockFromDB(cursor = cursor, treatyID = self.treatyID, condType = self.condType, blockIndex = blockIndex)
            
            editConditionBlocksMessage = EditConditionBlocksMessage(cursor = cursor, userID = self.userID, treatyID = self.treatyID, condType = self.condType)
            await interaction.response.edit_message(content = None, embed = editConditionBlocksMessage.embed, view = editConditionBlocksMessage.view)
            editConditionBlocksMessage.view.message = await interaction.original_response()

            conn.commit()

class DeleteAutoConditionModal(discord.ui.Modal):

    def __init__(self, userID: int, treatyID: int, condType: str, blockIndex: int):
        self.userID = userID
        self.treatyID = treatyID
        self.condType = condType
        self.blockIndex = blockIndex
        
        title = "Delete Condition"
        super().__init__(title=title)

        self.textInput: discord.ui.TextInput = discord.ui.TextInput(
            label = "Choose the condition no to delete",
            style = discord.TextStyle.short,
            placeholder = "Enter an integer...",
            required = True
        )
        self.add_item(self.textInput)
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.userID is None or interaction.user.id == self.userID: return await super().interaction_check(interaction)
        else: return False
    
    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None: # type: ignore
            import traceback
            traceback.print_exception(type(error), error, error.__traceback__)  # full traceback in console

            # respond to the user if possible
            if interaction.response.is_done():
                await interaction.followup.send(f"An error occured: {str(error)}", ephemeral=True)
            else:
                await interaction.response.send_message(f"An error occured: {str(error)}", ephemeral=True)
    
    async def on_submit(self, interaction: discord.Interaction):
        with sqlite3.connect(DB_PATH) as conn:
            cursor: sqlite3.Cursor = conn.cursor()

            if not self.textInput.value.isdigit(): raise ValueError("value entered is not positive integer")
            condIndex = int(self.textInput.value) - 1
            treaty_utils.DeleteAutoConditionFromDB(cursor = cursor, treatyID = self.treatyID, condType = self.condType, blockIndex = self.blockIndex, condIndex = condIndex)
            
            if self.condType == "clause":
                editClauseMessage = EditClauseMessage(cursor=cursor, userID = self.userID, treatyID = self.treatyID, clauseIndex = self.blockIndex)
                await interaction.response.edit_message(content = None, embed = editClauseMessage.embed, view = editClauseMessage.view)
                editClauseMessage.view.message = await interaction.original_response()
            else:
                editAutoConditionBlockMessage = EditAutoConditionBlockMessage(cursor = cursor, userID = self.userID, treatyID = self.treatyID, condType = self.condType, blockIndex = self.blockIndex)
                await interaction.response.edit_message(content = None, embed = editAutoConditionBlockMessage.embed, view = editAutoConditionBlockMessage.view)
                editAutoConditionBlockMessage.view.message = await interaction.original_response()

            conn.commit()

class DeleteVoteConditionModal(discord.ui.Modal):

    def __init__(self, userID: int, treatyID: int, condType: str, blockIndex: int):
        self.userID = userID
        self.treatyID = treatyID
        self.condType = condType
        self.blockIndex = blockIndex
        
        title = "Delete Condition"
        super().__init__(title=title)

        self.textInput: discord.ui.TextInput = discord.ui.TextInput(
            label = "Choose the condition no to delete",
            style = discord.TextStyle.short,
            placeholder = "Enter an integer...",
            required = True
        )
        self.add_item(self.textInput)
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.userID is None or interaction.user.id == self.userID: return await super().interaction_check(interaction)
        else: return False
    
    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None: # type: ignore
            import traceback
            traceback.print_exception(type(error), error, error.__traceback__)  # full traceback in console

            # respond to the user if possible
            if interaction.response.is_done():
                await interaction.followup.send(f"An error occured: {str(error)}", ephemeral=True)
            else:
                await interaction.response.send_message(f"An error occured: {str(error)}", ephemeral=True)
    
    async def on_submit(self, interaction: discord.Interaction):
        with sqlite3.connect(DB_PATH) as conn:
            cursor: sqlite3.Cursor = conn.cursor()

            if not self.textInput.value.isdigit(): raise ValueError("value entered is not positive integer")
            condIndex = int(self.textInput.value) - 1
            treaty_utils.DeleteVoteConditionFromDB(cursor = cursor, treatyID = self.treatyID, condType = self.condType, blockIndex = self.blockIndex, condIndex = condIndex)
            
            editVoteConditionBlockMessage = EditVoteConditionBlockMessage(cursor = cursor, userID = self.userID, treatyID = self.treatyID, condType = self.condType, blockIndex = self.blockIndex)
            await interaction.response.edit_message(content = None, embed = editVoteConditionBlockMessage.embed, view = editVoteConditionBlockMessage.view)
            editVoteConditionBlockMessage.view.message = await interaction.original_response()

            conn.commit()

class SelectClauseModal(discord.ui.Modal):

    def __init__(self, userID: int, treatyID: int):
        self.userID = userID
        self.treatyID = treatyID
        
        title = "Select Treaty Clause"
        super().__init__(title=title)

        self.textInput: discord.ui.TextInput = discord.ui.TextInput(
            label = "Choose the clause no to edit",
            style = discord.TextStyle.short,
            placeholder = "Enter an integer...",
            required = True
        )
        self.add_item(self.textInput)
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.userID is None or interaction.user.id == self.userID: return await super().interaction_check(interaction)
        else: return False
    
    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None: # type: ignore
            import traceback
            traceback.print_exception(type(error), error, error.__traceback__)  # full traceback in console

            # respond to the user if possible
            if interaction.response.is_done():
                await interaction.followup.send(f"An error occured: {str(error)}", ephemeral=True)
            else:
                await interaction.response.send_message(f"An error occured: {str(error)}", ephemeral=True)
    
    async def on_submit(self, interaction: discord.Interaction):
        with sqlite3.connect(DB_PATH) as conn:
            cursor: sqlite3.Cursor = conn.cursor()

            if not self.textInput.value.isdigit(): raise ValueError("value entered is not positive integer")
            clauseIndex = int(self.textInput.value) - 1
            
            editClauseMessage = EditClauseMessage(cursor=cursor, userID = self.userID, treatyID = self.treatyID, clauseIndex = clauseIndex)
            await interaction.response.edit_message(content = None, embed = editClauseMessage.embed, view = editClauseMessage.view)
            editClauseMessage.view.message = await interaction.original_response()

            conn.commit()

class SelectConditionBlockModal(discord.ui.Modal):

    def __init__(self, userID: int, treatyID: int, condType: str):
        self.userID = userID
        self.treatyID = treatyID
        self.condType = condType
        
        title = "Select Treaty Clause"
        super().__init__(title=title)

        self.textInput: discord.ui.TextInput = discord.ui.TextInput(
            label = "Choose the block no to edit",
            style = discord.TextStyle.short,
            placeholder = "Enter an integer...",
            required = True
        )
        self.add_item(self.textInput)
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.userID is None or interaction.user.id == self.userID: return await super().interaction_check(interaction)
        else: return False
    
    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None: # type: ignore
            import traceback
            traceback.print_exception(type(error), error, error.__traceback__)  # full traceback in console

            # respond to the user if possible
            if interaction.response.is_done():
                await interaction.followup.send(f"An error occured: {str(error)}", ephemeral=True)
            else:
                await interaction.response.send_message(f"An error occured: {str(error)}", ephemeral=True)
    
    async def on_submit(self, interaction: discord.Interaction):
        with sqlite3.connect(DB_PATH) as conn:
            cursor: sqlite3.Cursor = conn.cursor()

            if not self.textInput.value.isdigit(): raise ValueError("value entered is not positive integer")
            blockIndex = int(self.textInput.value) - 1
            
            blockType = treaty_utils.GetConditionBlockType(cursor = cursor, treatyID = self.treatyID, condType = self.condType, blockIndex = blockIndex)
            if blockType == "auto":
                editAutoConditionBlockMessage = EditAutoConditionBlockMessage(cursor = cursor, userID = self.userID, treatyID = self.treatyID, condType = self.condType, blockIndex = blockIndex)
                await interaction.response.edit_message(content = None, embed = editAutoConditionBlockMessage.embed, view = editAutoConditionBlockMessage.view)
                editAutoConditionBlockMessage.view.message = await interaction.original_response()
            elif blockType == "vote":
                editVoteConditionBlockMessage = EditVoteConditionBlockMessage(cursor = cursor, userID = self.userID, treatyID = self.treatyID, condType = self.condType, blockIndex = blockIndex)
                await interaction.response.edit_message(content = None, embed = editVoteConditionBlockMessage.embed, view = editVoteConditionBlockMessage.view)
                editVoteConditionBlockMessage.view.message = await interaction.original_response()
            else:
                raise ValueError("expected 'vote' or 'auto' as argument")

            conn.commit()

class EditTreatyMessage:

    def __init__(self, cursor: sqlite3.Cursor, userID: int, treatyID: int, timeout = 600):
        
        self.timeout = timeout

        treatyInfo = treaty_utils.GetAllTreatyStrings(cursor = cursor, treatyID = treatyID)
        treatyName = treatyInfo["name"]
        treatyClausesString = treatyInfo["fullText"]
        self.embed = discord.Embed(
            title = f"Editing '{treatyName}'",
            description = treatyClausesString
        )

        self.view = self.EditTreatyView(userID=userID, treatyID=treatyID, timeout=timeout)

    class EditTreatyView(discord.ui.View):
        def __init__(self, userID: int, treatyID: int, timeout):
            super().__init__(timeout=timeout)
            self.message: discord.Message | None = None
            self.userID = userID
            self.treatyID = treatyID
        
        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if self.userID is None or interaction.user.id == self.userID: return await super().interaction_check(interaction)
            else: return False
        
        async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
            import traceback
            traceback.print_exception(type(error), error, error.__traceback__)  # full traceback in console

            # respond to the user if possible
            if interaction.response.is_done():
                await interaction.followup.send(f"An error occured: {str(error)}", ephemeral=True)
            else:
                await interaction.response.send_message(f"An error occured: {str(error)}", ephemeral=True)
        
        async def on_timeout(self):
            if self.message or getattr(self.message, "view", None) is self:
                # disable all components
                for child in self.children:
                    child.disabled = True
                await self.message.edit(
                    content="Interaction Timed Out",
                    view=self
                )
        
        @discord.ui.button(label = u"\u2800Finalise Treaty\u2800", style = discord.ButtonStyle.green, row = 0)
        async def finalise_treaty_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
            with sqlite3.connect(DB_PATH) as conn:
                cursor: sqlite3.Cursor = conn.cursor()

                cursor.execute("""
                    UPDATE treaties
                    SET treaty_status = "Final"
                    WHERE treaty_id == (?)
                """, (self.treatyID,))
                viewTreatyMessage = ViewTreatyMessage(cursor=cursor, userID = self.userID, treatyID = self.treatyID)
                await interaction.response.edit_message(content = None, embed = viewTreatyMessage.embed, view = viewTreatyMessage.view)
                viewTreatyMessage.view.message = await interaction.original_response()

                conn.commit()
        
        @discord.ui.button(label = u"\u2800 Edit Clauses \u2800", style = discord.ButtonStyle.blurple, row = 0)
        async def edit_clauses_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
            with sqlite3.connect(DB_PATH) as conn:
                cursor: sqlite3.Cursor = conn.cursor()

                editClausesMessage = EditClausesMessage(cursor = cursor, userID = self.userID, treatyID = self.treatyID)
                await interaction.response.edit_message(content = None, embed = editClausesMessage.embed, view = editClausesMessage.view)
                editClausesMessage.view.message = await interaction.original_response()

                conn.commit()
        
        @discord.ui.button(label = u"\u2800Delete Treaty \u2800", style = discord.ButtonStyle.red, row = 0)
        async def delete_treaty_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
            await interaction.response.edit_message(content = "You clicked Delete Treaty Button", embed = None, view = None)
            with sqlite3.connect(DB_PATH) as conn:
                cursor: sqlite3.Cursor = conn.cursor()

                cursor.execute("""
                    DELETE FROM treaties
                    WHERE treaty_id == (?)
                """, (self.treatyID,))

                await interaction.response.edit_message(content = "Treaty Deleted", embed = None, view = None)

                conn.commit()
        
        @discord.ui.button(label = "\u2800Entry Into Force", style = discord.ButtonStyle.blurple, row = 1)
        async def entry_into_force_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
            with sqlite3.connect(DB_PATH) as conn:
                cursor: sqlite3.Cursor = conn.cursor()

                editConditionBlocksMessage = EditConditionBlocksMessage(cursor = cursor, userID = self.userID, treatyID = self.treatyID, condType = "entryIntoForce")
                await interaction.response.edit_message(content = None, embed = editConditionBlocksMessage.embed, view = editConditionBlocksMessage.view)
                editConditionBlocksMessage.view.message = await interaction.original_response()

                conn.commit()

        @discord.ui.button(label = u"\u2800 Suspension\u2800\u2800", style = discord.ButtonStyle.blurple, row = 1)
        async def suspension_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
            with sqlite3.connect(DB_PATH) as conn:
                cursor: sqlite3.Cursor = conn.cursor()

                editConditionBlocksMessage = EditConditionBlocksMessage(cursor = cursor, userID = self.userID, treatyID = self.treatyID, condType = "suspension")
                await interaction.response.edit_message(content = None, embed = editConditionBlocksMessage.embed, view = editConditionBlocksMessage.view)
                editConditionBlocksMessage.view.message = await interaction.original_response()

                conn.commit()

        @discord.ui.button(label = u"\u2800 Termination\u2800 \u2800", style = discord.ButtonStyle.blurple, row = 1)
        async def termination_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
            with sqlite3.connect(DB_PATH) as conn:
                cursor: sqlite3.Cursor = conn.cursor()

                editConditionBlocksMessage = EditConditionBlocksMessage(cursor = cursor, userID = self.userID, treatyID = self.treatyID, condType = "termination")
                await interaction.response.edit_message(content = None, embed = editConditionBlocksMessage.embed, view = editConditionBlocksMessage.view)
                editConditionBlocksMessage.view.message = await interaction.original_response()

                conn.commit()

        @discord.ui.button(label = u"\u2800 Participation \u2800", style = discord.ButtonStyle.blurple, row = 2)
        async def participation_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
            with sqlite3.connect(DB_PATH) as conn:
                cursor: sqlite3.Cursor = conn.cursor()

                editConditionBlocksMessage = EditConditionBlocksMessage(cursor = cursor, userID = self.userID, treatyID = self.treatyID, condType = "participation")
                await interaction.response.edit_message(content = None, embed = editConditionBlocksMessage.embed, view = editConditionBlocksMessage.view)
                editConditionBlocksMessage.view.message = await interaction.original_response()

                conn.commit()

        @discord.ui.button(label = u"\u2800 Withdrawal\u2800\u2800", style = discord.ButtonStyle.blurple, row = 2)
        async def withdrawal_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
            with sqlite3.connect(DB_PATH) as conn:
                cursor: sqlite3.Cursor = conn.cursor()

                editConditionBlocksMessage = EditConditionBlocksMessage(cursor = cursor, userID = self.userID, treatyID = self.treatyID, condType = "withdrawal")
                await interaction.response.edit_message(content = None, embed = editConditionBlocksMessage.embed, view = editConditionBlocksMessage.view)
                editConditionBlocksMessage.view.message = await interaction.original_response()

                conn.commit()

        @discord.ui.button(label = u"\u2800 \u2800Expulsion \u2800 \u2800", style = discord.ButtonStyle.blurple, row = 2)
        async def expulsion_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
            with sqlite3.connect(DB_PATH) as conn:
                cursor: sqlite3.Cursor = conn.cursor()

                editConditionBlocksMessage = EditConditionBlocksMessage(cursor = cursor, userID = self.userID, treatyID = self.treatyID, condType = "expulsion")
                await interaction.response.edit_message(content = None, embed = editConditionBlocksMessage.embed, view = editConditionBlocksMessage.view)
                editConditionBlocksMessage.view.message = await interaction.original_response()

                conn.commit()

class SelectClauseCategoryMessage:

    def __init__(self, cursor: sqlite3.Cursor, userID: int, treatyID: int, timeout = 600):
        
        self.timeout = timeout

        treatyInfo = treaty_utils.GetAllTreatyStrings(cursor = cursor, treatyID = treatyID)
        treatyName = treatyInfo["name"]
        
        self.embed = discord.Embed(
            title = f"Adding Clause to '{treatyName}'"
        )

        self.view = self.SelectClauseCategoryView(userID=userID, treatyID=treatyID, timeout=timeout)

    class SelectClauseCategoryView(discord.ui.View):
        def __init__(self, userID: int, treatyID: int, timeout):
            super().__init__(timeout=timeout)
            self.message: discord.Message | None = None
            self.userID = userID
            self.treatyID = treatyID

            options = [
                discord.SelectOption(label = "Military", value = "Military"),
                discord.SelectOption(label = "Economic", value = "Economic")
            ]

            add_clause_dropdown: discord.ui.Select = discord.ui.Select(
                placeholder = "Select a category...",
                options = options
            )
            add_clause_dropdown.callback = self.select_clause_category_dropdown_callback # type: ignore
            self.add_item(add_clause_dropdown)
        
        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if self.userID is None or interaction.user.id == self.userID: return await super().interaction_check(interaction)
            else: return False
        
        async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
            import traceback
            traceback.print_exception(type(error), error, error.__traceback__)  # full traceback in console

            # respond to the user if possible
            if interaction.response.is_done():
                await interaction.followup.send(f"An error occured: {str(error)}", ephemeral=True)
            else:
                await interaction.response.send_message(f"An error occured: {str(error)}", ephemeral=True)
        
        async def on_timeout(self):
            if self.message or getattr(self.message, "view", None) is self:
                # disable all components
                for child in self.children:
                    child.disabled = True
                await self.message.edit(
                    content="Interaction Timed Out",
                    view=self
                )
        
        async def select_clause_category_dropdown_callback(self, interaction : discord.Interaction):
            with sqlite3.connect(DB_PATH) as conn:
                cursor: sqlite3.Cursor = conn.cursor()

                data = cast(dict, interaction.data)
                values = data["values"]
                selected_value = values[0]
                addClauseMessage = AddClauseMessage(cursor=cursor, userID=self.userID, treatyID=self.treatyID, clauseCategory=selected_value)
                await interaction.response.edit_message(content = None, embed = addClauseMessage.embed, view = addClauseMessage.view)
                addClauseMessage.view.message = await interaction.original_response()

                conn.commit()

class AddClauseMessage:

    def __init__(self, cursor: sqlite3.Cursor, userID: int, treatyID: int, clauseCategory: str, timeout = 600):
        
        self.timeout = timeout

        treatyInfo = treaty_utils.GetAllTreatyStrings(cursor = cursor, treatyID = treatyID)
        treatyName = treatyInfo["name"]
        
        self.embed = discord.Embed(
            title = f"Adding Clause to '{treatyName}'"
        )

        self.view = self.AddClauseView(userID=userID, treatyID=treatyID, clauseCategory=clauseCategory, timeout=timeout)

    class AddClauseView(discord.ui.View):
        def __init__(self, userID: int, treatyID: int, clauseCategory: str, timeout):
            super().__init__(timeout=timeout)
            self.message: discord.Message | None = None
            self.userID = userID
            self.treatyID = treatyID

            labelDict = treaty_utils.GetClauseCategoryLabels(clauseCategory=clauseCategory)
            options = [discord.SelectOption(label=label, value=str(value)) for value, label in labelDict.items()]

            addClauseDropdown: discord.ui.Select = discord.ui.Select(
                placeholder = "Select a clause...",
                options = options
            )
            addClauseDropdown.callback = self.add_clause_dropdown_callback # type: ignore
            self.add_item(addClauseDropdown)
        
        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if self.userID is None or interaction.user.id == self.userID: return await super().interaction_check(interaction)
            else: return False
        
        async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
            import traceback
            traceback.print_exception(type(error), error, error.__traceback__)  # full traceback in console

            # respond to the user if possible
            if interaction.response.is_done():
                await interaction.followup.send(f"An error occured: {str(error)}", ephemeral=True)
            else:
                await interaction.response.send_message(f"An error occured: {str(error)}", ephemeral=True)
        
        async def on_timeout(self):
            if self.message or getattr(self.message, "view", None) is self:
                # disable all components
                for child in self.children:
                    child.disabled = True
                await self.message.edit(
                    content="Interaction Timed Out",
                    view=self
                )
        
        async def add_clause_dropdown_callback(self, interaction : discord.Interaction):
            data = cast(dict, interaction.data)
            values = data["values"]
            selectedValue = values[0]
            clauseEnum = TreatyClauses(int(selectedValue))
            addTreatyClauseModal = AddClauseModal(userID=self.userID, treatyID=self.treatyID, clauseEnum=clauseEnum)
            await interaction.response.send_modal(addTreatyClauseModal)

class AddAutoConditionMessage:

    def __init__(self, cursor: sqlite3.Cursor, userID: int, treatyID: int, condType: str, blockIndex: int, timeout = 600):
        
        self.timeout = timeout

        treatyInfo = treaty_utils.GetAllTreatyStrings(cursor = cursor, treatyID = treatyID)
        treatyName = treatyInfo["name"]
        
        self.embed = discord.Embed(
            title = f"Adding Condition to '{treatyName}'"
        )

        self.view = self.AddAutoConditionView(cursor = cursor, userID=userID, treatyID=treatyID, condType = condType, blockIndex = blockIndex, timeout = timeout)

    class AddAutoConditionView(discord.ui.View):
        def __init__(self, cursor: sqlite3.Cursor, userID: int, treatyID: int, condType: str, blockIndex: int, timeout):
            super().__init__(timeout=timeout)
            self.message: discord.Message | None = None
            self.userID = userID
            self.treatyID = treatyID
            self.condType = condType
            self.blockIndex = blockIndex

            if condType == "clause":
                clauseEnum = treaty_utils.GetClauseEnumFromIndex(cursor = cursor, treatyID = treatyID, clauseIndex = blockIndex)
            else:
                clauseEnum = None

            labelDict = treaty_utils.GetAutoConditionLabels(condType = condType, clauseEnum = clauseEnum)
            options = [discord.SelectOption(label=label, value=str(value)) for value, label in labelDict.items()]

            addConditionDropdown: discord.ui.Select = discord.ui.Select(
                placeholder = "Select a condition...",
                options = options
            )
            addConditionDropdown.callback = self.add_condition_dropdown_callback # type: ignore
            self.add_item(addConditionDropdown)
        
        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if self.userID is None or interaction.user.id == self.userID: return await super().interaction_check(interaction)
            else: return False
        
        async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
            import traceback
            traceback.print_exception(type(error), error, error.__traceback__)  # full traceback in console

            # respond to the user if possible
            if interaction.response.is_done():
                await interaction.followup.send(f"An error occured: {str(error)}", ephemeral=True)
            else:
                await interaction.response.send_message(f"An error occured: {str(error)}", ephemeral=True)
        
        async def on_timeout(self):
            if self.message or getattr(self.message, "view", None) is self:
                # disable all components
                for child in self.children:
                    child.disabled = True
                await self.message.edit(
                    content="Interaction Timed Out",
                    view=self
                )
        
        async def add_condition_dropdown_callback(self, interaction : discord.Interaction):
            data = cast(dict, interaction.data)
            values = data["values"]
            selectedValue = values[0]
            condEnum = TreatyConditions(int(selectedValue))
            addTreatyConditionModal = AddAutoConditionModal(userID=self.userID, treatyID=self.treatyID, condType = self.condType, condEnum=condEnum, blockIndex = self.blockIndex)
            await interaction.response.send_modal(addTreatyConditionModal)

class EditClauseMessage:

    def __init__(self, cursor: sqlite3.Cursor, userID: int, treatyID: int, clauseIndex: int, timeout = 600):
        
        self.timeout = timeout

        cursor.execute("""
            SELECT treaty_args
            FROM treaties
            WHERE treaty_id == (?)
        """, (treatyID,))
        treatyArgs = cursor.fetchone()
        if treatyArgs is None: raise ValueError(f"treaty_args column for treaty with ID {treatyID} is null")
        treatyArgs = json.loads(treatyArgs[0])
        clauses = treatyArgs["clauses"]
        if len(clauses) <= clauseIndex: raise ValueError(f"clauseIndex {clauseIndex} given does not exist")
        clause = clauses[clauseIndex]

        descString = ""
        clauseString = treaty_utils.GetClauseString(cursor = cursor, clauseEnum = TreatyClauses(clause["clauseEnum"]), clauseArgs = clause["clauseArgs"])
        descString += f"{clauseIndex+1}. {clauseString}\n"
        if clause.get("conditions") is None: clause["conditions"] = []
        conditions = clause["conditions"]
        j = 0
        for condition in conditions:
            j += 1
            condString = treaty_utils.GetAutoConditionString(cursor = cursor, condEnum = TreatyConditions(condition["condEnum"]), condArgs = condition["condArgs"])
            descString += f"\t{j}) {condString}\n"
        
        self.embed = discord.Embed(
            title = "Editing Clause:",
            description = descString
        )

        self.view = self.EditClauseView(userID=userID, treatyID=treatyID, clauseIndex=clauseIndex, timeout=timeout)

    class EditClauseView(discord.ui.View):
        def __init__(self, userID: int, treatyID: int, clauseIndex: int, timeout):
            super().__init__(timeout=timeout)
            self.message: discord.Message | None = None
            self.userID = userID
            self.treatyID = treatyID
            self.clauseIndex = clauseIndex
        
        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if self.userID is None or interaction.user.id == self.userID: return await super().interaction_check(interaction)
            else: return False
        
        async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
            import traceback
            traceback.print_exception(type(error), error, error.__traceback__)  # full traceback in console

            # respond to the user if possible
            if interaction.response.is_done():
                await interaction.followup.send(f"An error occured: {str(error)}", ephemeral=True)
            else:
                await interaction.response.send_message(f"An error occured: {str(error)}", ephemeral=True)
        
        async def on_timeout(self):
            if self.message or getattr(self.message, "view", None) is self:
                # disable all components
                for child in self.children:
                    child.disabled = True
                await self.message.edit(
                    content="Interaction Timed Out",
                    view=self
                )
        
        @discord.ui.button(label = "Add Condition", style = discord.ButtonStyle.green, row = 0)
        async def add_condition_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
            with sqlite3.connect(DB_PATH) as conn:
                cursor: sqlite3.Cursor = conn.cursor()

                addConditionMessage = AddAutoConditionMessage(cursor = cursor, userID = self.userID, treatyID = self.treatyID, condType = "clause", blockIndex = self.clauseIndex)
                await interaction.response.edit_message(content = None, embed = addConditionMessage.embed, view = addConditionMessage.view)
                addConditionMessage.view.message = await interaction.original_response()

                conn.commit()
        
        @discord.ui.button(label = "Delete Condition", style = discord.ButtonStyle.red, row = 0)
        async def delete_condition_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
            deleteAutoConditionModal = DeleteAutoConditionModal(userID = self.userID, treatyID = self.treatyID, condType = "clause", blockIndex = self.clauseIndex)
            await interaction.response.send_modal(deleteAutoConditionModal)
        
        @discord.ui.button(label = "Edit Clauses", style = discord.ButtonStyle.blurple, row = 1)
        async def edit_clauses_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
            with sqlite3.connect(DB_PATH) as conn:
                cursor: sqlite3.Cursor = conn.cursor()

                editClausesMessage = EditClausesMessage(cursor = cursor, userID = self.userID, treatyID = self.treatyID)
                await interaction.response.edit_message(content = None, embed = editClausesMessage.embed, view = editClausesMessage.view)
                editClausesMessage.view.message = await interaction.original_response()

                conn.commit()

class EditAutoConditionBlockMessage:

    def __init__(self, cursor: sqlite3.Cursor, userID: int, treatyID: int, condType: str, blockIndex: int, timeout = 600):
        
        self.timeout = timeout

        cursor.execute("""
            SELECT treaty_args
            FROM treaties
            WHERE treaty_id == (?)
        """, (treatyID,))
        treatyArgs = cursor.fetchone()
        if treatyArgs is None: raise ValueError(f"treaty_args column for treaty with ID {treatyID} is null")
        treatyArgs = json.loads(treatyArgs[0])

        blocks = treatyArgs[condType]
        if len(blocks) <= blockIndex: raise ValueError(f"blockIndex {blockIndex} given does not exist")
        block = blocks[blockIndex]
        
        if block["blockType"] != "auto": raise ValueError("expected auto for blockType")

        descString = f"{blockIndex+1}. All of the following:\n"
        if block.get("conditions") is None: block["conditions"] = []
        conditions = block["conditions"]
        j = 0
        for condition in conditions:
            j += 1
            condString = treaty_utils.GetAutoConditionString(cursor = cursor, condEnum = TreatyConditions(condition["condEnum"]), condArgs = condition["condArgs"])
            descString += f"\t{j}) {condString}\n"
        
        self.embed = discord.Embed(
            title = "Editing Auto Condition Block:",
            description = descString
        )

        self.view = self.EditAutoConditionBlockView(userID = userID, treatyID = treatyID, condType = condType, blockIndex = blockIndex, timeout = timeout)

    class EditAutoConditionBlockView(discord.ui.View):
        def __init__(self, userID: int, treatyID: int, condType: str, blockIndex: int, timeout):
            super().__init__(timeout=timeout)
            self.message: discord.Message | None = None
            self.userID = userID
            self.treatyID = treatyID
            self.blockIndex = blockIndex
            self.condType = condType
        
        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if self.userID is None or interaction.user.id == self.userID: return await super().interaction_check(interaction)
            else: return False
        
        async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
            import traceback
            traceback.print_exception(type(error), error, error.__traceback__)  # full traceback in console

            # respond to the user if possible
            if interaction.response.is_done():
                await interaction.followup.send(f"An error occured: {str(error)}", ephemeral=True)
            else:
                await interaction.response.send_message(f"An error occured: {str(error)}", ephemeral=True)
        
        async def on_timeout(self):
            if self.message or getattr(self.message, "view", None) is self:
                # disable all components
                for child in self.children:
                    child.disabled = True
                await self.message.edit(
                    content="Interaction Timed Out",
                    view=self
                )
        
        @discord.ui.button(label = "Add Condition", style = discord.ButtonStyle.green, row = 0)
        async def add_condition_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
            with sqlite3.connect(DB_PATH) as conn:
                cursor: sqlite3.Cursor = conn.cursor()

                addConditionMessage = AddAutoConditionMessage(cursor = cursor, userID = self.userID, treatyID = self.treatyID, condType = self.condType, blockIndex = self.blockIndex)
                await interaction.response.edit_message(content = None, embed = addConditionMessage.embed, view = addConditionMessage.view)
                addConditionMessage.view.message = await interaction.original_response()

                conn.commit()
        
        @discord.ui.button(label = "Delete Condition", style = discord.ButtonStyle.red, row = 0)
        async def delete_condition_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
            deleteAutoConditionModal = DeleteAutoConditionModal(userID = self.userID, treatyID = self.treatyID, condType = self.condType, blockIndex = self.blockIndex)
            await interaction.response.send_modal(deleteAutoConditionModal)
        
        @discord.ui.button(label = "Edit Condition Blocks", style = discord.ButtonStyle.blurple, row = 1)
        async def edit_condition_blocks_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
            with sqlite3.connect(DB_PATH) as conn:
                cursor: sqlite3.Cursor = conn.cursor()

                editConditionBlocksMessage = EditConditionBlocksMessage(cursor = cursor, userID = self.userID, treatyID = self.treatyID, condType = self.condType)
                await interaction.response.edit_message(content = None, embed = editConditionBlocksMessage.embed, view = editConditionBlocksMessage.view)
                editConditionBlocksMessage.view.message = await interaction.original_response()

                conn.commit()

class EditVoteConditionBlockMessage:

    def __init__(self, cursor: sqlite3.Cursor, userID: int, treatyID: int, condType: str, blockIndex: int, timeout = 600):
        
        self.timeout = timeout

        cursor.execute("""
            SELECT treaty_args
            FROM treaties
            WHERE treaty_id == (?)
        """, (treatyID,))
        treatyArgs = cursor.fetchone()
        if treatyArgs is None: raise ValueError(f"treaty_args column for treaty with ID {treatyID} is null")
        treatyArgs = json.loads(treatyArgs[0])

        blocks = treatyArgs[condType]
        if len(blocks) <= blockIndex: raise ValueError(f"blockIndex {blockIndex} given does not exist")
        block = blocks[blockIndex]
        
        if block["blockType"] != "vote": raise ValueError("expected vote for blockType")

        descString = treaty_utils.GetVoteConditionBlockString(cursor = cursor, voteArgs = block["voteArgs"], conditions = block["conditions"])
        
        self.embed = discord.Embed(
            title = "Editing Vote Condition Block:",
            description = f"{blockIndex+1}. " + descString
        )

        self.view = self.EditVoteConditionBlockView(userID = userID, treatyID = treatyID, condType = condType, blockIndex = blockIndex, timeout = timeout)

    class EditVoteConditionBlockView(discord.ui.View):
        def __init__(self, userID: int, treatyID: int, condType: str, blockIndex: int, timeout):
            super().__init__(timeout=timeout)
            self.message: discord.Message | None = None
            self.userID = userID
            self.treatyID = treatyID
            self.blockIndex = blockIndex
            self.condType = condType
        
        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if self.userID is None or interaction.user.id == self.userID: return await super().interaction_check(interaction)
            else: return False
        
        async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
            import traceback
            traceback.print_exception(type(error), error, error.__traceback__)  # full traceback in console

            # respond to the user if possible
            if interaction.response.is_done():
                await interaction.followup.send(f"An error occured: {str(error)}", ephemeral=True)
            else:
                await interaction.response.send_message(f"An error occured: {str(error)}", ephemeral=True)
        
        async def on_timeout(self):
            if self.message or getattr(self.message, "view", None) is self:
                # disable all components
                for child in self.children:
                    child.disabled = True
                await self.message.edit(
                    content="Interaction Timed Out",
                    view=self
                )
        
        @discord.ui.button(label = "Add Condition", style = discord.ButtonStyle.green, row = 0)
        async def add_condition_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
            addVoteConditionModal = AddVoteConditionModal(userID = self.userID, treatyID = self.treatyID, condType = self.condType, blockIndex = self.blockIndex)
            await interaction.response.send_modal(addVoteConditionModal)
        
        @discord.ui.button(label = "Delete Condition", style = discord.ButtonStyle.red, row = 0)
        async def delete_condition_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
            deleteVoteConditionModal = DeleteVoteConditionModal(userID = self.userID, treatyID = self.treatyID, condType = self.condType, blockIndex = self.blockIndex)
            await interaction.response.send_modal(deleteVoteConditionModal)
        
        @discord.ui.button(label = "Edit Condition Blocks", style = discord.ButtonStyle.blurple, row = 1)
        async def edit_condition_blocks_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
            with sqlite3.connect(DB_PATH) as conn:
                cursor: sqlite3.Cursor = conn.cursor()

                editConditionBlocksMessage = EditConditionBlocksMessage(cursor = cursor, userID = self.userID, treatyID = self.treatyID, condType = self.condType)
                await interaction.response.edit_message(content = None, embed = editConditionBlocksMessage.embed, view = editConditionBlocksMessage.view)
                editConditionBlocksMessage.view.message = await interaction.original_response()

                conn.commit()

class AddClauseModal(discord.ui.Modal):

    def __init__(self, userID: int, treatyID: int, clauseEnum: TreatyClauses):
        self.userID = userID
        self.treatyID = treatyID
        self.clauseEnum = clauseEnum
        
        title = "Add Treaty Clause: " + treaty_utils.GetClauseLabel(clauseEnum)
        super().__init__(title=title)

        self.textInputs = treaty_utils.GetClauseTextInputs(clauseEnum)
        for textInput in self.textInputs.values():
            self.add_item(textInput)
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.userID is None or interaction.user.id == self.userID: return await super().interaction_check(interaction)
        else: return False
    
    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None: # type: ignore
            import traceback
            traceback.print_exception(type(error), error, error.__traceback__)  # full traceback in console

            # respond to the user if possible
            if interaction.response.is_done():
                await interaction.followup.send(f"An error occured: {str(error)}", ephemeral=True)
            else:
                await interaction.response.send_message(f"An error occured: {str(error)}", ephemeral=True)
    
    async def on_submit(self, interaction: discord.Interaction):
        with sqlite3.connect(DB_PATH) as conn:
            cursor: sqlite3.Cursor = conn.cursor()

            clauseArgs = treaty_utils.GetClauseArgs(cursor = cursor, clauseEnum = self.clauseEnum, textInputs = self.textInputs)
            treaty_utils.AddClauseToDB(cursor = cursor, treatyID = self.treatyID, clauseEnum = self.clauseEnum, clauseArgs = clauseArgs)
            
            editClausesMessage = EditClausesMessage(cursor=cursor, userID = self.userID, treatyID = self.treatyID)
            await interaction.response.edit_message(content = None, embed = editClausesMessage.embed, view = editClausesMessage.view)
            editClausesMessage.view.message = await interaction.original_response()

            conn.commit()

class AddAutoConditionModal(discord.ui.Modal):

    def __init__(self, userID: int, treatyID: int, condType: str, condEnum: TreatyConditions, blockIndex: int):
        self.userID = userID
        self.treatyID = treatyID
        self.condType = condType
        self.condEnum = condEnum
        self.blockIndex = blockIndex
        
        title = "Add Condition: " + treaty_utils.GetAutoConditionLabel(condEnum)
        super().__init__(title=title)

        self.textInputs = treaty_utils.GetAutoConditionTextInputs(condEnum)
        for textInput in self.textInputs.values():
            self.add_item(textInput)
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.userID is None or interaction.user.id == self.userID: return await super().interaction_check(interaction)
        else: return False
    
    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None: # type: ignore
            import traceback
            traceback.print_exception(type(error), error, error.__traceback__)  # full traceback in console

            # respond to the user if possible
            if interaction.response.is_done():
                await interaction.followup.send(f"An error occured: {str(error)}", ephemeral=True)
            else:
                await interaction.response.send_message(f"An error occured: {str(error)}", ephemeral=True)
    
    async def on_submit(self, interaction: discord.Interaction):
        with sqlite3.connect(DB_PATH) as conn:
            cursor: sqlite3.Cursor = conn.cursor()

            condArgs = treaty_utils.GetAutoConditionArgs(cursor = cursor, condEnum = self.condEnum, textInputs = self.textInputs)
            treaty_utils.AddAutoConditionToDB(cursor = cursor, treatyID = self.treatyID, condType = self.condType, condEnum = self.condEnum, condArgs = condArgs, blockIndex = self.blockIndex)
            
            if self.condType == "clause":
                editClauseMessage = EditClauseMessage(cursor=cursor, userID = self.userID, treatyID = self.treatyID, clauseIndex = self.blockIndex)
                await interaction.response.edit_message(content = None, embed = editClauseMessage.embed, view = editClauseMessage.view)
                editClauseMessage.view.message = await interaction.original_response()
            else:
                editAutoConditionBlockMessage = EditAutoConditionBlockMessage(cursor=cursor, userID = self.userID, treatyID = self.treatyID, condType = self.condType, blockIndex = self.blockIndex)
                await interaction.response.edit_message(content = None, embed = editAutoConditionBlockMessage.embed, view = editAutoConditionBlockMessage.view)
                editAutoConditionBlockMessage.view.message = await interaction.original_response()

            conn.commit()

class AddVoteConditionBlockModal(discord.ui.Modal):

    def __init__(self, userID: int, treatyID: int, condType: str):
        self.userID = userID
        self.treatyID = treatyID
        self.condType = condType
        
        title = "Add Vote Condition Block"
        super().__init__(title=title)

        textInputs: dict[str,discord.ui.TextInput] = {}

        textInputs["requiredPercentage"] = discord.ui.TextInput(
            label = "Required percentage for votes to pass",
            style = discord.TextStyle.short,
            placeholder = "Enter Integer...",
            required = True)
        
        textInputs["participantCountries"] = discord.ui.TextInput(
            label = "List of countries who can vote",
            style = discord.TextStyle.long,
            placeholder = "Enter comma-separated country names...\nPut 'all' for All Signatories",
            required = True)
        
        textInputs["vetoCountries"] = discord.ui.TextInput(
            label = "List of countries who can veto the vote",
            style = discord.TextStyle.long,
            placeholder = "Enter comma-separated country names...\nPut 'all' for All Signatories",
            required = False)
        
        textInputs["callCountries"] = discord.ui.TextInput(
            label = "List of countries who can call the vote",
            style = discord.TextStyle.long,
            placeholder = "Enter comma-separated country names...\nPut 'all' for All Signatories",
            required = True)
        
        self.textInputs = textInputs
        for textInput in self.textInputs.values():
            self.add_item(textInput)
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.userID is None or interaction.user.id == self.userID: return await super().interaction_check(interaction)
        else: return False
    
    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None: # type: ignore
            import traceback
            traceback.print_exception(type(error), error, error.__traceback__)  # full traceback in console

            # respond to the user if possible
            if interaction.response.is_done():
                await interaction.followup.send(f"An error occured: {str(error)}", ephemeral=True)
            else:
                await interaction.response.send_message(f"An error occured: {str(error)}", ephemeral=True)
    
    async def on_submit(self, interaction: discord.Interaction):
        with sqlite3.connect(DB_PATH) as conn:
            cursor: sqlite3.Cursor = conn.cursor()

            voteArgs = treaty_utils.GetVoteArgs(cursor = cursor, textInputs = self.textInputs)
            blockIndex = treaty_utils.AddVoteConditionBlockToDB(cursor = cursor, treatyID = self.treatyID, condType = self.condType, voteArgs = voteArgs)
            
            editVoteConditionBlockMessage = EditVoteConditionBlockMessage(cursor = cursor, userID = self.userID, treatyID = self.treatyID, condType = self.condType, blockIndex = blockIndex)
            await interaction.response.edit_message(content = None, embed = editVoteConditionBlockMessage.embed, view = editVoteConditionBlockMessage.view)
            editVoteConditionBlockMessage.view.message = await interaction.original_response()

            conn.commit()

class AddVoteConditionModal(discord.ui.Modal):

    def __init__(self, userID: int, treatyID: int, condType: str, blockIndex: int):
        self.userID = userID
        self.treatyID = treatyID
        self.condType = condType
        self.blockIndex = blockIndex
        
        title = "Add Custom Condition"
        super().__init__(title=title)

        textInputs: dict[str,discord.ui.TextInput] = {}

        textInputs["condition"] = discord.ui.TextInput(
            label = "Custom condition to add for vote",
            style = discord.TextStyle.short,
            placeholder = "Enter a sentence",
            required = True)
        
        self.textInputs = textInputs
        for textInput in self.textInputs.values():
            self.add_item(textInput)
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.userID is None or interaction.user.id == self.userID: return await super().interaction_check(interaction)
        else: return False
    
    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None: # type: ignore
            import traceback
            traceback.print_exception(type(error), error, error.__traceback__)  # full traceback in console

            # respond to the user if possible
            if interaction.response.is_done():
                await interaction.followup.send(f"An error occured: {str(error)}", ephemeral=True)
            else:
                await interaction.response.send_message(f"An error occured: {str(error)}", ephemeral=True)
    
    async def on_submit(self, interaction: discord.Interaction):
        with sqlite3.connect(DB_PATH) as conn:
            cursor: sqlite3.Cursor = conn.cursor()

            condition = self.textInputs["condition"].value
            treaty_utils.AddVoteConditionToDB(cursor = cursor, treatyID = self.treatyID, condType = self.condType, condString = condition, blockIndex = self.blockIndex)
            
            editVoteConditionBlockMessage = EditVoteConditionBlockMessage(cursor = cursor, userID = self.userID, treatyID = self.treatyID, condType = self.condType, blockIndex = self.blockIndex)
            await interaction.response.edit_message(content = None, embed = editVoteConditionBlockMessage.embed, view = editVoteConditionBlockMessage.view)
            editVoteConditionBlockMessage.view.message = await interaction.original_response()

            conn.commit()

async def setup(bot: commands.Bot):
    await bot.add_cog(Treaties(bot))