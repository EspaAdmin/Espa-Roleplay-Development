import discord
import json
import sqlite3
import datetime
from dateutil.relativedelta import relativedelta # type: ignore
from enum import Enum
from typing import Any
from difflib import get_close_matches

class TreatyClauses(Enum):
    # Treaty clauses, comments use [] for input fields and () for optional additions
    # Where non-signatories not specified, only countries who have ratified the treaty will be affected
    
    ## Military
    CEDE_LAND = 1 # [Country] cedes [State List] to [Country]
    CEASEFIRE = 2 # [Country List] cease hostilities
    FORMAL_PEACE = 3 # [Country List] declare a formal end to any war between them
    NON_AGGRESSION = 4 # [Country List] will not engage in aggression against eachother
    MUTUAL_DEFENSE = 5 # [Country List] will agree to come to eachother's aid against any act of war
    DEMILITARISE_ZONE = 6 # [Country] will not place any military troops in [State List]
    MILITARY_ACCESS = 7 # [Country List] will provide military access to [Country List]
    GUARANTEE_INDEPENDENCE = 8 # [Country List] will guarantee [Country List]'s independence
    DECLARE_WAR = 9 # [Country List] will declare war on [Country List - non-signatories]

    ##Economic
    PAY_MONEY = 21 # [Country] will pay [Country] [Money] (every [Int] year(s))
    EMBARGO = 22 # [Country List] will place an embargo on [Country List - non-signatories]

class TreatyConditions(Enum):
    # Treaty conditions, comments use [] for input fields

    ## Treaty Specific
    AFTER_TIME_EIF = 21 # Has been more than [Int] years after Entry Into Force
    BEFORE_TIME_EIF = 22 # Has been less than [Int] years after Entry Into Force
    AFTER_DATE = 23 # Current date is after [Date]
    BEFORE_DATE = 24 # Current date is before [Date]
    SIGNATORIES_INCLUDED = 25 # Signatories include [Country List]
    SIGNATORIES_NO = 26 # Treaty has [at least/at most] [Int] signatories

    ## Signatory Specific
    AT_WAR_WITH = 41 # Signatory [is/is not] at war with [Country List]
    OTHER_TREATY_MEMBER = 42 # Signatory [is/is not] a member of [Treaty]
    IN_COUNTRY_LIST = 43 # Signatory [is/is not] one of [Country List]

## UNFINISHED
# Put this in Generic Utils class
def GetCurrentGameDateString() -> str:
    # Get data from table
    return "01/01/1950"

# Put this in Generic Utils class
def GetDateValue(dateStr: str) -> datetime.date:
    return datetime.datetime.strptime(dateStr, '%d/%m/%Y').date()

# Put this in Generic Utils class
def GetCountryIDFromUser(cursor: sqlite3.Cursor, userID: int) -> int:

    cursor.execute(f"""
        SELECT nation_id
        FROM playernations
        WHERE PlayerIDs LIKE (?)
    """, (f"%{userID}%",))
    countryID = cursor.fetchone()
    if countryID is None:
        raise ValueError(f"no country ID found from discord userID {userID}")
    countryID = countryID[0] # Turn singleton tuple to value
    
    return countryID

def UpdateTreatyStatus(cursor: sqlite3.Cursor, treatyID: int, newStatus: str):
    if newStatus not in ["Draft", "Final", "InForce", "Terminated", "Suspended"]: raise ValueError(f"invalid treaty status'{newStatus}'")

    cursor.execute(f"""
        UPDATE treaties
        SET treaty_status = (?)
        WHERE treaty_id == (?)
    """, (newStatus, treatyID, ))

def EnterTreatyIntoForce(cursor: sqlite3.Cursor, treatyID: int) -> None:
    cursor.execute("""
        SELECT treaty_args
        FROM treaties
        WHERE treaty_id == (?)
    """, (treatyID,))
    row = cursor.fetchone()
    if row is None: raise ValueError(f"no treaty with ID {treatyID} found")
    treatyArgs = row[0]
    
    if treatyArgs is None: raise ValueError(f"treaty_args column for treaty with ID {treatyID} is null")
    treatyArgs = json.loads(treatyArgs)
    
    treatyArgs["inForceDate"] = GetCurrentGameDateString()

    treatyArgs = json.dumps(treatyArgs)

    cursor.execute("""
        UPDATE treaties
        SET treaty_args = (?)
        WHERE treaty_id == (?)
    """, (treatyArgs, treatyID))

    UpdateTreatyStatus(cursor = cursor, treatyID = treatyID, newStatus = "InForce")

def CheckAutoCondBlocks(cursor: sqlite3.Cursor, treatyID: int, condType: str, countryID: int) -> bool:
    if condType not in ["entryIntoForce", "suspension", "termination", "participation", "withdrawal", "expulsion"]: raise ValueError("Invalid condType")
    
    cursor.execute(f"""
        SELECT treaty_name, treaty_args, treaty_status
        FROM treaties
        WHERE treaty_id == (?)
    """, (treatyID, ))
    treatyRow = cursor.fetchone()

    if treatyRow is None: raise ValueError(f"no treaty with treaty ID {treatyID} found")

    treatyName = treatyRow[0]
    if treatyName is None: raise ValueError(f"name of treaty with ID {treatyID} is null")

    treatyStatus = treatyRow[2]
    if treatyStatus is None: raise ValueError(f"treaty_status column for treaty with ID {treatyID} is null")
    if treatyStatus in ["Draft"]: return False

    treatyArgs = treatyRow[1]
    if treatyArgs is None: raise ValueError(f"treaty_args column for treaty with ID {treatyID} is null")
    treatyArgs = json.loads(treatyArgs)

    if treatyArgs.get(condType) is None: treatyArgs[condType] = []
    condBlocks = treatyArgs[condType]

    if len(condBlocks) == 0:
        # Default behaviour for participation is no conditions required, for entryIntoForce is at least one signatory, rest not allowed with no cond blocks
        if condType == "participation":
            return True
        elif condType == "entryIntoForce" and len(GetAllSignatories(cursor = cursor, treatyID = treatyID)) >= 1:
            return True
        else:
            return False

    for condBlock in condBlocks:
        if condBlock["blockType"] == "auto":
            conditions = condBlock["conditions"]
            if all(CheckAutoCondition(cursor = cursor, treatyID = treatyID, condEnum = TreatyConditions(condition["condEnum"]), condArgs = condition["condArgs"], signatoryID = countryID) for condition in conditions):
                return True
    
    return False

## UNFINISHED CASES
def CheckAutoCondition(cursor: sqlite3.Cursor, treatyID: int, condEnum: TreatyConditions, condArgs: dict[str,Any], signatoryID: int | None = None) -> bool:
    cursor.execute(f"""
        SELECT treaty_name, treaty_args, treaty_status
        FROM treaties
        WHERE treaty_id == (?)
    """, (treatyID, ))
    treatyRow = cursor.fetchone()

    if treatyRow is None: raise ValueError(f"no treaty with treaty ID {treatyID} found")

    treatyName = treatyRow[0]
    if treatyName is None: raise ValueError(f"name of treaty with ID {treatyID} is null")

    treatyStatus = treatyRow[2]
    if treatyStatus is None: raise ValueError(f"treaty_status column for treaty with ID {treatyID} is null")

    treatyArgs = treatyRow[1]
    if treatyArgs is None: raise ValueError(f"treaty_args column for treaty with ID {treatyID} is null")
    treatyArgs = json.loads(treatyArgs)
    
    isCondTrue: bool
    match(condEnum):
        case TreatyConditions.AFTER_TIME_EIF:
            if treatyStatus in ["Draft","Final"]:
                isCondTrue = False
            else:
                inForceDate = GetDateValue(treatyArgs["inForceDate"])
                currentDate = GetDateValue(GetCurrentGameDateString())
                yearDiff = relativedelta(currentDate, inForceDate).years

                yearNo = condArgs["yearNo"]
                if yearDiff >= yearNo:
                    isCondTrue = True
                else:
                    isCondTrue = False
        case TreatyConditions.BEFORE_TIME_EIF:
            inForceDate = GetDateValue(treatyArgs["inForceDate"])
            currentDate = GetDateValue(GetCurrentGameDateString())
            yearDiff = relativedelta(currentDate - inForceDate).years

            yearNo = condArgs["yearNo"]
            if yearDiff >= yearNo:
                isCondTrue = False
            else:
                isCondTrue = True
        case TreatyConditions.AFTER_DATE:
            afterDate = GetDateValue(condArgs["afterDate"])
            currentDate = GetDateValue(GetCurrentGameDateString())

            if currentDate > afterDate:
                isCondTrue = True
            else:
                isCondTrue = False
        case TreatyConditions.BEFORE_DATE:
            beforeDate = GetDateValue(condArgs["beforeDate"])
            currentDate = GetDateValue(GetCurrentGameDateString())

            if currentDate < beforeDate:
                isCondTrue = True
            else:
                isCondTrue = False
        case TreatyConditions.SIGNATORIES_INCLUDED:
            signatoryIDs = GetAllSignatories(cursor = cursor, treatyID = treatyID)
            requiredSignatoryIDs = condArgs["signatoryCountryIDs"]

            if set(requiredSignatoryIDs).issubset(signatoryIDs):
                isCondTrue = True
            else:
                isCondTrue = False
        case TreatyConditions.SIGNATORIES_NO:
            signatoryIDs = GetAllSignatories(cursor = cursor, treatyID = treatyID)
            inequality = condArgs["inequality"]
            requiredSignatoryNo = condArgs["signatoriesNo"]

            if inequality == "at least":
                if len(signatoryIDs) >= requiredSignatoryNo: 
                    isCondTrue = True
                else: 
                    isCondTrue = False
            elif inequality == "at most":
                if len(signatoryIDs) <= requiredSignatoryNo: 
                    isCondTrue = True
                else: 
                    isCondTrue = False
        case TreatyConditions.AT_WAR_WITH:
            raise NotImplementedError("Treaty Clause AT_WAR_WITH not implemented in CheckAutoCondition()")
        case TreatyConditions.OTHER_TREATY_MEMBER:
            isNegative = condArgs["isNegative"]
            otherTreatyID = condArgs["otherTreatyID"]

            cursor.execute("""
                SELECT signed_treaties
                FROM playernations
                WHERE nation_id == (?)
            """, (signatoryID,))
            signedTreaties = cursor.fetchone()
            if signedTreaties is None: raise ValueError(f"country with nation_id {signatoryID} does not exist")
            signedTreaties = signedTreaties[0]
            if signedTreaties is None: raise ValueError(f"country with nation_id {signatoryID} has null signed_treaties column")
            signedTreaties = json.loads(signedTreaties)

            isCondTrue = isNegative
            for treatyInfo in signedTreaties:
                if treatyInfo["treatyID"] == otherTreatyID:
                    isCondTrue = not isNegative
        case TreatyConditions.IN_COUNTRY_LIST:
            isNegative = condArgs["isNegative"]
            possibleCountryIDs = condArgs["possibleCountryIDs"]

            if signatoryID in possibleCountryIDs:
               isCondTrue = not isNegative
            else:
               isCondTrue = isNegative
        case _:
            raise ValueError(f"no case is defined for {condEnum} in CheckAutoCondition()")
    
    return isCondTrue

def GetAllSignatories(cursor: sqlite3.Cursor, treatyID: int) -> list[int]:

    signatoryList = []

    cursor.execute("""
        SELECT nation_id, signed_treaties
        FROM playernations
    """)
    countryRows = cursor.fetchall()
    if countryRows is None: raise ValueError("no country rows found in database")
    for countryRow in countryRows:
        signedTreatiesList = json.loads(countryRow[1])
        for treatyInfo in signedTreatiesList:
            signedTreatyID = treatyInfo["treatyID"]
            if signedTreatyID == treatyID and countryRow[0] not in signatoryList:
                signatoryList.append(countryRow[0])

    return signatoryList

def GetAllTreatyStrings(cursor: sqlite3.Cursor, treatyID: int) -> dict[str,str]:
    
    treatyInfo = {"fullText": ""}

    cursor.execute(f"""
        SELECT treaty_name, treaty_args, treaty_status
        FROM treaties
        WHERE treaty_id == (?)
    """, (treatyID,))
    treatyRow = cursor.fetchone()
    if treatyRow is None: raise ValueError(f"no treaty with treaty ID {treatyID} found")

    treatyName = treatyRow[0]
    if treatyName is None: raise ValueError(f"name of treaty with ID {treatyID} is null")

    treatyStatus = treatyRow[2]
    if treatyStatus is None: raise ValueError(f"treaty_status column for treaty with ID {treatyID} is null")

    treatyArgs = treatyRow[1]
    if treatyArgs is None: raise ValueError(f"treaty_args column for treaty with ID {treatyID} is null")
    treatyArgs = json.loads(treatyArgs)
    if treatyArgs.get("clauses") is None: treatyArgs["clauses"] = []
    clauses = treatyArgs["clauses"]

    clausesString = "Clauses:\n"
    i = 0
    for clause in clauses:
        i += 1
        clauseString = GetClauseString(cursor = cursor, clauseEnum = TreatyClauses(clause["clauseEnum"]), clauseArgs = clause["clauseArgs"])
        clausesString += f"{i}. {clauseString}\n"
        if clause.get("conditions") is None: clause["conditions"] = []
        j = 0
        for condition in clause["conditions"]:
            j += 1
            conditionString = GetAutoConditionString(cursor = cursor, condEnum = TreatyConditions(condition["condEnum"]), condArgs = condition["condArgs"])
            clausesString += f"\t{j}) {conditionString}\n"
    treatyInfo["clauses"] = clausesString
    treatyInfo["fullText"] += f"{clausesString}\n"
    
    condTypes = ["entryIntoForce", "suspension", "termination", "participation", "withdrawal", "expulsion"]
    defaultStrings = {
        "entryIntoForce": "Treaty has at least 1 signatory",
        "suspension": "Suspension not allowed",
        "termination": "Termination not allowed",
        "participation": "No conditions required",
        "withdrawal": "Withdrawal not allowed",
        "expulsion": "Expulsion not allowed"
    }
    headerStrings = {
        "entryIntoForce": "Conditions For Entry Into Force:",
        "suspension": "Conditions For Suspension:",
        "termination": "Conditions For Termination:",
        "participation": "Conditions For Participation:",
        "withdrawal": "Conditions For Withdrawal:",
        "expulsion": "Conditions For Expulsion:"
    }

    for condType in condTypes:
        if treatyArgs.get(condType) is None: treatyArgs[condType] = []
        condBlocks = treatyArgs[condType]

        allBlocksString = f"{headerStrings[condType]}\n"
        i = 0
        if not condBlocks:
            allBlocksString += f". {defaultStrings[condType]}\n"
        else:
            for condBlock in condBlocks:
                i += 1
                blockType = condBlock["blockType"]
                if blockType == "auto":
                    conditions = condBlock["conditions"]
                    if len(conditions) == 1:
                        conditionString = GetAutoConditionString(cursor = cursor, condEnum = TreatyConditions(conditions[0]["condEnum"]), condArgs = conditions[0]["condArgs"])
                        allBlocksString += f"{i}. {conditionString}\n"
                    else:
                        allBlocksString += f"{i}. All of the following:\n"
                        j = 0
                        for condition in conditions:
                            j += 1
                            conditionString = GetAutoConditionString(cursor = cursor, condEnum = TreatyConditions(condition["condEnum"]), condArgs = condition["condArgs"])
                            allBlocksString += f"\t{j}) {conditionString}\n"
                elif blockType == "vote":
                    voteBlockString = GetVoteConditionBlockString(cursor = cursor, voteArgs = condBlock["voteArgs"], conditions = condBlock["conditions"])
                    allBlocksString += f"{i}. {voteBlockString}"
                else:
                    raise ValueError(f"unexpected block type '{blockType}' encountered")
        
        treatyInfo[condType] = allBlocksString
        treatyInfo["fullText"] += f"{allBlocksString}\n"
    
    treatyInfo["name"] = treatyName
    treatyInfo["status"] = treatyStatus

    return treatyInfo

def GetClauseString(cursor: sqlite3.Cursor, clauseEnum: TreatyClauses, clauseArgs: dict[str,Any]) -> str:
    class ClauseStringHelper:
        @staticmethod
        def GetCountryNamesString(cursor: sqlite3.Cursor, countryIDList: list[int]) -> str:
            placeholderString = ','.join('?' * len(countryIDList))
            cursor.execute(f"""
                SELECT name
                From playernations
                WHERE nation_id IN ({placeholderString})
            """, tuple(countryIDList))
            countryNames = cursor.fetchall()

            if countryNames is None:
                countryIDsListString = ','.join([str(x) for x in countryIDList])
                raise ValueError(f"Out of country IDs ({countryIDsListString}) no country names were found")
            elif len(countryIDList) != len(countryNames) or countryNames.count((None,)) > 0:
                countryIDsListString = ','.join([str(x) for x in countryIDList])
                raise ValueError(f"Out of country IDs ({countryIDsListString}) only {len(countryNames) - countryNames.count((None,))} country names were found")
            
            countryNames = [countryNameTuple[0] for countryNameTuple in countryNames]
            if len(countryNames) == 1: countryNamesString = " " + countryNames[0]
            else: countryNamesString = ','.join([" " + countryName for countryName in countryNames[:-1]]) + f" and {countryNames[-1]}"

            return countryNamesString[1:]
        
        @staticmethod
        def GetCountryName(cursor: sqlite3.Cursor, countryID: int) -> str:
            cursor.execute("""
                SELECT name
                FROM playernations
                WHERE nation_id == (?)
            """, (countryID,))
            countryName = cursor.fetchone()
            if countryName is None: raise ValueError(f"country ID {countryID} not found")
            countryName = countryName[0]
            if countryName is None: raise ValueError(f"no country name found for country ID {countryID}")

            return countryName

        @staticmethod
        def GetStateNamesString(cursor: sqlite3.Cursor, stateIDList: list[int]) -> str:
            placeholderString = ','.join('?' * len(stateIDList))
            cursor.execute(f"""
                SELECT name
                From states
                WHERE state_id IN ({placeholderString})
            """, tuple(stateIDList))
            stateNames = cursor.fetchall()

            if stateNames is None:
                stateIDListString = ','.join([str(x) for x in stateIDList])
                raise ValueError(f"Out of state IDs ({stateIDListString}) no state names were found")
            elif len(stateIDList) != len(stateNames) or stateNames.count((None,)) > 0:
                stateIDListString = ','.join([str(x) for x in stateIDList])
                raise ValueError(f"Out of state IDs ({stateIDListString}) only {len(stateNames) - stateNames.count((None,))} state names were found")
            
            stateNames = [stateNameTuple[0] for stateNameTuple in stateNames]
            if len(stateNames) == 1: stateNamesString = " " + stateNames[0]
            else: stateNamesString = ','.join([" " + countryName for countryName in stateNames[:-1]]) + f" and {stateNames[-1]}"

            return stateNamesString[1:]
        
        @staticmethod
        def GetStateName(cursor: sqlite3.Cursor, stateID: int) -> str:
            cursor.execute("""
                SELECT name
                FROM states
                WHERE state_id == (?)
            """, (stateID,))
            stateName = cursor.fetchone()
            if stateName is None: raise ValueError(f"state ID {stateID} not found")
            stateName = stateName[0]
            if stateName is None: raise ValueError(f"no state name found for state ID {stateID}")

            return stateName

    treatyClauseString = ""
    match(clauseEnum):
        case TreatyClauses.CEDE_LAND:
            losingCountryID = clauseArgs["losingCountryID"]
            lostStateIDs = clauseArgs["lostStateIDs"]
            gainingCountryID = clauseArgs["gainingCountryID"]

            if not isinstance(lostStateIDs, list):
                raise TypeError("lostStateIDs parameter in CEDE_LAND treaty clause is not a list")
            elif len(lostStateIDs) == 0:
                raise ValueError("lostStateIDs parameter in CEDE_LAND treaty clause has a length of 0")
            
            losingCountryName = ClauseStringHelper.GetCountryName(cursor, losingCountryID)
            gainingCountryName = ClauseStringHelper.GetCountryName(cursor, gainingCountryID)
            lostStateNames = ClauseStringHelper.GetStateNamesString(cursor, lostStateIDs)

            treatyClauseString = f"{losingCountryName} cedes states {lostStateNames} to {gainingCountryName}"
        case TreatyClauses.CEASEFIRE:
            ceasefireCountryIDs = clauseArgs["ceasefireCountryIDs"]

            if ceasefireCountryIDs == "all": ceasefireCountryNames = "All Signatories"
            else: ceasefireCountryNames = ClauseStringHelper.GetCountryNamesString(cursor, ceasefireCountryIDs)

            treatyClauseString = f"{ceasefireCountryNames} will cease hostilities"
        case TreatyClauses.FORMAL_PEACE:
            peaceCountryIDs = clauseArgs["peaceCountryIDs"]

            if peaceCountryIDs == 'all': peaceCountryNames = "All Signatories"
            else: peaceCountryNames = ClauseStringHelper.GetCountryNamesString(cursor, peaceCountryIDs)

            treatyClauseString = f"{peaceCountryNames} declare a formal end to any war between them"
        case TreatyClauses.NON_AGGRESSION:
            nonAggressionCountryIDs = clauseArgs["nonAggressionCountryIDs"]

            if nonAggressionCountryIDs == 'all': nonAggressionCountryNames = "All Signatories"
            else: nonAggressionCountryNames = ClauseStringHelper.GetCountryNamesString(cursor, nonAggressionCountryIDs)

            treatyClauseString = f"{nonAggressionCountryNames} will not engage in aggression against eachother"
        case TreatyClauses.MUTUAL_DEFENSE:
            defensiveCountryIDs = clauseArgs["defensiveCountryIDs"]

            if defensiveCountryIDs == 'all': defensiveCountryNames = "All Signatories"
            else: defensiveCountryNames = ClauseStringHelper.GetCountryNamesString(cursor, defensiveCountryIDs)

            treatyClauseString = f"{defensiveCountryNames} will agree to come to eachother's aid against any act of war"
        case TreatyClauses.DEMILITARISE_ZONE:
            demilCountryID = clauseArgs["demilCountryID"]
            demilStateIDs = clauseArgs["demilStateIDs"]

            if not isinstance(demilStateIDs, list):
                raise TypeError("demilStateIDs parameter in DEMILITARISE_ZONE treaty clause is not a list")
            elif len(demilStateIDs) == 0:
                raise ValueError("demilStateIDs parameter in DEMILITARISE_ZONE treaty clause has a length of 0")
            
            demilCountryName = ClauseStringHelper.GetCountryName(cursor, demilCountryID)
            demilStateNames = ClauseStringHelper.GetStateNamesString(cursor, demilStateIDs)

            treatyClauseString = f"{demilCountryName} will demilitarise {demilStateNames}"
        case TreatyClauses.MILITARY_ACCESS:
            accessingCountryIDs = clauseArgs["accessingCountryIDs"]
            accessedCountryIDs = clauseArgs["accessedCountryIDs"]

            if accessingCountryIDs == 'all': accessingCountryNames = "All Signatories"
            else: accessingCountryNames = ClauseStringHelper.GetCountryNamesString(cursor, accessingCountryIDs)
            if accessedCountryIDs == 'all': accessedCountryNames = "All Signatories"
            else: accessedCountryNames = ClauseStringHelper.GetCountryNamesString(cursor, accessedCountryIDs)

            treatyClauseString = f"{accessedCountryNames} will provide military access to {accessingCountryNames}"
        case TreatyClauses.GUARANTEE_INDEPENDENCE:
            guarantorCountryIDs = clauseArgs["guarantorCountryIDs"]
            guaranteedCountryIDs = clauseArgs["guaranteedCountryIDs"]

            if guarantorCountryIDs == 'all': guarantorCountryNames = "All Signatories"
            else: guarantorCountryNames = ClauseStringHelper.GetCountryNamesString(cursor, guarantorCountryIDs)
            if guaranteedCountryIDs == 'all': guaranteedCountryNames = "All Signatories"
            else: guaranteedCountryNames = ClauseStringHelper.GetCountryNamesString(cursor, guaranteedCountryIDs)

            treatyClauseString = f"{guarantorCountryNames} will guarantee the independence of {guaranteedCountryNames}"
        case TreatyClauses.DECLARE_WAR:
            alliedCountryIDs = clauseArgs["alliedCountryIDs"]
            opposingCountryIDs = clauseArgs["opposingCountryIDs"]

            if alliedCountryIDs == 'all': alliedCountryNames = "All Signatories"
            else: alliedCountryNames = ClauseStringHelper.GetCountryNamesString(cursor, alliedCountryIDs)
            opposingCountryNames = ClauseStringHelper.GetCountryNamesString(cursor, opposingCountryIDs)

            treatyClauseString = f"{alliedCountryNames} will declare war on {opposingCountryNames}"
        case TreatyClauses.PAY_MONEY:
            givingCountryID = clauseArgs["givingCountryID"]
            receivingCountryID = clauseArgs["receivingCountryID"]
            moneyGiven = clauseArgs["moneyGiven"]
            yearFrequency = clauseArgs["yearFrequency"]

            givingCountryName = ClauseStringHelper.GetCountryName(cursor, givingCountryID)
            receivingCountryName = ClauseStringHelper.GetCountryName(cursor, receivingCountryID)

            treatyClauseString = f"{givingCountryName} will pay {receivingCountryName} ${moneyGiven}"

            if yearFrequency is not None:
                if yearFrequency == 1:
                    treatyClauseString += " every year"
                elif yearFrequency > 1:
                    treatyClauseString += f" every {yearFrequency} years"
        case TreatyClauses.EMBARGO:
            embargoingCountryIDs = clauseArgs["embargoingCountryIDs"]
            embargoedCountryIDs = clauseArgs["embargoedCountryIDs"]

            if embargoingCountryIDs == 'all': embargoingCountryNames = "All Signatories"
            else: embargoingCountryNames = ClauseStringHelper.GetCountryNamesString(cursor, embargoingCountryIDs)
            embargoedCountryNames = ClauseStringHelper.GetCountryNamesString(cursor, embargoedCountryIDs)

            treatyClauseString = f"{embargoingCountryNames} will place an embargo on {embargoedCountryNames}"
        case _:
            raise ValueError(f"no case is defined for {clauseEnum} in GetTreatyClauseString()")
    
    return treatyClauseString

def GetAutoConditionString(cursor: sqlite3.Cursor, condEnum: TreatyConditions, condArgs: dict[str,Any]) -> str:
    class CondStringHelper:
        @staticmethod
        def GetCountryNamesString(cursor: sqlite3.Cursor, countryIDList: list[int]) -> str:
            placeholderString = ','.join('?' * len(countryIDList))
            cursor.execute(f"""
                SELECT name
                From playernations
                WHERE nation_id IN ({placeholderString})
            """, tuple(countryIDList))
            countryNames = cursor.fetchall()

            if countryNames is None:
                countryIDsListString = ','.join([str(x) for x in countryIDList])
                raise ValueError(f"Out of country IDs ({countryIDsListString}) no country names were found")
            elif len(countryIDList) != len(countryNames) or countryNames.count((None,)) > 0:
                countryIDsListString = ','.join([str(x) for x in countryIDList])
                raise ValueError(f"Out of country IDs ({countryIDsListString}) only {len(countryNames) - countryNames.count((None,))} country names were found")
            
            countryNames = [countryNameTuple[0] for countryNameTuple in countryNames]
            if len(countryNames) == 1: countryNamesString = " " + countryNames[0]
            else: countryNamesString = ','.join([" " + countryName for countryName in countryNames[:-1]]) + f" and {countryNames[-1]}"

            return countryNamesString[1:]
        
        @staticmethod
        def GetCountryName(cursor: sqlite3.Cursor, countryID: int) -> str:
            cursor.execute("""
                SELECT name
                FROM playernations
                WHERE nation_id == (?)
            """, (countryID,))
            countryName = cursor.fetchone()
            if countryName is None: raise ValueError(f"country ID {countryID} not found")
            countryName = countryName[0]
            if countryName is None: raise ValueError(f"no country name found for country ID {countryID}")

            return countryName

        @staticmethod
        def GetStateNamesString(cursor: sqlite3.Cursor, stateIDList: list[int]) -> str:
            placeholderString = ','.join('?' * len(stateIDList))
            cursor.execute(f"""
                SELECT name
                From states
                WHERE state_id IN ({placeholderString})
            """, tuple(stateIDList))
            stateNames = cursor.fetchall()

            if stateNames is None:
                stateIDListString = ','.join([str(x) for x in stateIDList])
                raise ValueError(f"Out of state IDs ({stateIDListString}) no state names were found")
            elif len(stateIDList) != len(stateNames) or stateNames.count((None,)) > 0:
                stateIDListString = ','.join([str(x) for x in stateIDList])
                raise ValueError(f"Out of state IDs ({stateIDListString}) only {len(stateNames) - stateNames.count((None,))} state names were found")
            
            stateNames = [stateNameTuple[0] for stateNameTuple in stateNames]
            if len(stateNames) == 1: stateNamesString = " " + stateNames[0]
            else: stateNamesString = ','.join([" " + countryName for countryName in stateNames[:-1]]) + f" and {stateNames[-1]}"

            return stateNamesString[1:]
        
        @staticmethod
        def GetStateName(cursor: sqlite3.Cursor, stateID: int) -> str:
            cursor.execute("""
                SELECT name
                FROM states
                WHERE state_id == (?)
            """, (stateID,))
            stateName = cursor.fetchone()
            if stateName is None: raise ValueError(f"state ID {stateID} not found")
            stateName = stateName[0]
            if stateName is None: raise ValueError(f"no state name found for state ID {stateID}")

            return stateName

        @staticmethod
        def GetTreatyName(cursor:sqlite3.Cursor, treatyID: int) -> str:
            cursor.execute("""
                SELECT treaty_name
                FROM treaties
                WHERE treaty_id == (?)
            """, (treatyID, ))
            treatyName = cursor.fetchone()
            if treatyName is None: raise ValueError(f"treatyID {treatyID} not found")
            treatyName = treatyName[0]
            if treatyName is None: raise ValueError(f"no treaty name found for treaty ID {treatyID}")

            return treatyName

    treatyCondString = ""
    match(condEnum):
        case TreatyConditions.AFTER_TIME_EIF:
            yearNo = condArgs["yearNo"]

            treatyCondString = f"Has been more than {yearNo} years In Force"
        case TreatyConditions.BEFORE_TIME_EIF:
            yearNo = condArgs["yearNo"]

            treatyCondString = f"Has been less than {yearNo} years In Force"
        case TreatyConditions.AFTER_DATE:
            afterDate = condArgs["afterDate"]

            treatyCondString = f"Current date is after {afterDate}"
        case TreatyConditions.BEFORE_DATE:
            beforeDate = condArgs["beforeDate"]

            treatyCondString = f"Current date is before {beforeDate}"
        case TreatyConditions.SIGNATORIES_INCLUDED:
            signatoryCountryIDs = condArgs["signatoryCountryIDs"]

            signatoryCountryNames = CondStringHelper.GetCountryNamesString(cursor, signatoryCountryIDs)

            treatyCondString = f"Signatories include {signatoryCountryNames}"
        case TreatyConditions.SIGNATORIES_NO:
            inequality = condArgs["inequality"]
            signatoriesNo = condArgs["signatoriesNo"]

            if inequality not in ["at least","at most"]: raise ValueError("inequality argument must be 'at least' or 'at most'")
            
            treatyCondString = f"Treaty has {inequality} {signatoriesNo} Signatories"
        case TreatyConditions.AT_WAR_WITH:
            isNegative = condArgs["isNegative"]
            warCountryIDs = condArgs["warCountryIDs"]

            warCountryNames = CondStringHelper.GetCountryNamesString(cursor, warCountryIDs)

            if isNegative: formatString = "is not"
            else: formatString = "is"

            treatyCondString = f"Signatory {formatString} at war with {warCountryNames}"
        case TreatyConditions.OTHER_TREATY_MEMBER:
            isNegative = condArgs["isNegative"]
            otherTreatyID = condArgs["otherTreatyID"]

            otherTreatyName = CondStringHelper.GetTreatyName(cursor, otherTreatyID)

            if isNegative: formatString = "is not"
            else: formatString = "is"

            treatyCondString = f"Signatory {formatString} a member of {otherTreatyName}"
        case TreatyConditions.IN_COUNTRY_LIST:
            isNegative = condArgs["isNegative"]
            possibleCountryIDs = condArgs["possibleCountryIDs"]

            possibleCountryNames = CondStringHelper.GetCountryNamesString(cursor, possibleCountryIDs)

            if isNegative: formatString = "is not"
            else: formatString = "is"

            treatyCondString = f"Signatory {formatString} one of {possibleCountryNames}"
        case _:
            raise ValueError(f"no case is defined for {condEnum} in GetAutoConditionString()")
    
    return treatyCondString

def GetVoteConditionBlockString(cursor: sqlite3.Cursor, voteArgs: dict[str,Any], conditions: list[str]) -> str:
    class VoteCondStringHelper:
        @staticmethod
        def GetCountryNamesString(cursor: sqlite3.Cursor, countryIDList: list[int], isAnd: bool) -> str:
            placeholderString = ','.join('?' * len(countryIDList))
            cursor.execute(f"""
                SELECT name
                From playernations
                WHERE nation_id IN ({placeholderString})
            """, tuple(countryIDList))
            countryNames = cursor.fetchall()

            lastJoiner: str
            if isAnd: lastJoiner = "and"
            else: lastJoiner = "or"

            if countryNames is None:
                countryIDsListString = ','.join([str(x) for x in countryIDList])
                raise ValueError(f"Out of country IDs ({countryIDsListString}) no country names were found")
            elif len(countryIDList) != len(countryNames) or countryNames.count((None,)) > 0:
                countryIDsListString = ','.join([str(x) for x in countryIDList])
                raise ValueError(f"Out of country IDs ({countryIDsListString}) only {len(countryNames) - countryNames.count((None,))} country names were found")
            
            countryNames = [countryNameTuple[0] for countryNameTuple in countryNames]
            if len(countryNames) == 1: countryNamesString = " " + countryNames[0]
            else: countryNamesString = ','.join([" " + countryName for countryName in countryNames[:-1]]) + f" {lastJoiner} {countryNames[-1]}"

            return countryNamesString[1:]
        
        @staticmethod
        def GetCountryName(cursor: sqlite3.Cursor, countryID: int) -> str:
            cursor.execute("""
                SELECT name
                FROM playernations
                WHERE nation_id == (?)
            """, (countryID,))
            countryName = cursor.fetchone()
            if countryName is None: raise ValueError(f"country ID {countryID} not found")
            countryName = countryName[0]
            if countryName is None: raise ValueError(f"no country name found for country ID {countryID}")

            return countryName

        @staticmethod
        def GetStateNamesString(cursor: sqlite3.Cursor, stateIDList: list[int]) -> str:
            placeholderString = ','.join('?' * len(stateIDList))
            cursor.execute(f"""
                SELECT name
                From states
                WHERE state_id IN ({placeholderString})
            """, tuple(stateIDList))
            stateNames = cursor.fetchall()

            if stateNames is None:
                stateIDListString = ','.join([str(x) for x in stateIDList])
                raise ValueError(f"Out of state IDs ({stateIDListString}) no state names were found")
            elif len(stateIDList) != len(stateNames) or stateNames.count((None,)) > 0:
                stateIDListString = ','.join([str(x) for x in stateIDList])
                raise ValueError(f"Out of state IDs ({stateIDListString}) only {len(stateNames) - stateNames.count((None,))} state names were found")
            
            stateNames = [stateNameTuple[0] for stateNameTuple in stateNames]
            if len(stateNames) == 1: stateNamesString = " " + stateNames[0]
            else: stateNamesString = ','.join([" " + countryName for countryName in stateNames[:-1]]) + f" and {stateNames[-1]}"

            return stateNamesString[1:]
        
        @staticmethod
        def GetStateName(cursor: sqlite3.Cursor, stateID: int) -> str:
            cursor.execute("""
                SELECT name
                FROM states
                WHERE state_id == (?)
            """, (stateID,))
            stateName = cursor.fetchone()
            if stateName is None: raise ValueError(f"state ID {stateID} not found")
            stateName = stateName[0]
            if stateName is None: raise ValueError(f"no state name found for state ID {stateID}")

            return stateName

    
    voteConditionBlockString = ""

    participantCountryIDs = voteArgs["participantCountryIDs"]
    requiredPercentage: int = voteArgs["requiredPercentage"]
    vetoCountryIDs = voteArgs["vetoCountryIDs"]
    callerIDs = voteArgs["callCountryIDs"]

    voteConditionBlockString += f"Passage of a vote by {requiredPercentage}% approval\n"

    if len(vetoCountryIDs) > 0:
        if callerIDs == "all": 
            callerNames = "any Signatory"
        else: 
            callerNames = VoteCondStringHelper.GetCountryNamesString(cursor = cursor, countryIDList = callerIDs, isAnd = False)
        voteConditionBlockString += f"\t- called by {callerNames},\n"
    
    if participantCountryIDs == "all":
        participantNames = "all Signatories"
    else:
        participantNames = VoteCondStringHelper.GetCountryNamesString(cursor = cursor, countryIDList = participantCountryIDs, isAnd = True)
    voteConditionBlockString += f"\t- participated in by {participantNames},\n"

    if len(vetoCountryIDs) > 0:
        if vetoCountryIDs == "all":
            vetoNames = "any Signatory"
        else:
            vetoNames = VoteCondStringHelper.GetCountryNamesString(cursor = cursor, countryIDList = vetoCountryIDs, isAnd = False)
        voteConditionBlockString += f"\t- provided it is not vetoed by {vetoNames},\n"
    
    if len(conditions) > 0:
        voteConditionBlockString += f"\t- based on the following conditions being true:\n"
        i = 0
        for condition in conditions:
            i += 1
            voteConditionBlockString += f"\t\t{i}) {condition}\n"

    return voteConditionBlockString

def GetClauseLabel(clauseEnum: TreatyClauses) -> str:
    match(clauseEnum):
        case TreatyClauses.CEDE_LAND: return "Cede Land"
        case TreatyClauses.CEASEFIRE: return "Ceasefire"
        case TreatyClauses.FORMAL_PEACE: return "Formal Peace"
        case TreatyClauses.NON_AGGRESSION: return "Non-aggression"
        case TreatyClauses.MUTUAL_DEFENSE: return "Defensive Pact"
        case TreatyClauses.DEMILITARISE_ZONE: return "Demilitarised Zone"
        case TreatyClauses.MILITARY_ACCESS: return "Military Access"
        case TreatyClauses.GUARANTEE_INDEPENDENCE: return "Independence Guarantee"
        case TreatyClauses.DECLARE_WAR: return "Joint War Declaration"
        case TreatyClauses.PAY_MONEY: return "Pay Money"
        case TreatyClauses.EMBARGO: return "Joint Embargo"
        case _: raise ValueError(f"no case is defined for {clauseEnum} in GetTreatyClauseLabel()")

def GetAutoConditionLabel(condEnum: TreatyConditions) -> str:
    match(condEnum):
        case TreatyConditions.AFTER_TIME_EIF: return "After Years In Force"
        case TreatyConditions.BEFORE_TIME_EIF: return "Before Years In Force"
        case TreatyConditions.AFTER_DATE: return "After Date"
        case TreatyConditions.BEFORE_DATE: return "Before Date"
        case TreatyConditions.SIGNATORIES_INCLUDED: return "Specific Signatories Included"
        case TreatyConditions.SIGNATORIES_NO: return "Number of Signatories"
        case TreatyConditions.AT_WAR_WITH: return "At War With Country(s)"
        case TreatyConditions.OTHER_TREATY_MEMBER: return "In Other Treaty"
        case TreatyConditions.IN_COUNTRY_LIST: return "In Country List"
        case _: raise ValueError(f"no case is defined for {condEnum} in GetTreatyConditionLabel()")

def GetClauseCategoryLabels(clauseCategory: str | None = None) -> dict[int,str]:
    labelDict = {}
    for clauseEnum in TreatyClauses:
        if (
            clauseCategory is None
            or (clauseCategory == "Military" and clauseEnum.value > 0 and clauseEnum.value <= 20)
            or (clauseCategory == "Economic" and clauseEnum.value > 20 and clauseEnum.value <= 40)
        ):
            labelDict[clauseEnum.value] = GetClauseLabel(clauseEnum)
    return labelDict

def GetAutoConditionLabels(condType: str, clauseEnum: TreatyClauses | None = None) -> dict[int,str]:
    clauseExclusions: dict[TreatyClauses,list[TreatyConditions]] = {
        TreatyClauses.CEDE_LAND: [TreatyConditions.BEFORE_DATE, TreatyConditions.BEFORE_TIME_EIF],
        TreatyClauses.FORMAL_PEACE: [TreatyConditions.BEFORE_DATE, TreatyConditions.BEFORE_TIME_EIF],
        TreatyClauses.DECLARE_WAR: [TreatyConditions.BEFORE_DATE, TreatyConditions.BEFORE_TIME_EIF]
    }
    for clauseEnum in TreatyClauses: 
        if clauseExclusions.get(clauseEnum) is None: 
            clauseExclusions[clauseEnum] = []
    
    condTypeExclusions: dict[str,list[TreatyConditions]] = {
        "clause": [],
        "entryIntoForce": [TreatyConditions.AFTER_TIME_EIF, TreatyConditions.BEFORE_TIME_EIF, TreatyConditions.BEFORE_DATE],
        "termination": [TreatyConditions.BEFORE_DATE, TreatyConditions.BEFORE_TIME_EIF],
        "participation": [],
        "withdrawal": [TreatyConditions.BEFORE_DATE, TreatyConditions.BEFORE_TIME_EIF]
    }
    
    labelDict = {}
    for condEnum in TreatyConditions:
        if (
            (
                (condType == "clause" and condEnum.value > 20 and condEnum.value <= 40 and clauseEnum is not None and condEnum not in clauseExclusions[clauseEnum])
                or (condType == "entryIntoForce" and condEnum.value > 20 and condEnum.value <= 40)
                or (condType == "termination" and condEnum.value > 20 and condEnum.value <= 40)
                or (condType == "participation" and condEnum.value > 40 and condEnum.value <= 60)
                or (condType == "withdrawal" and condEnum.value > 20 and condEnum.value <= 60)
            )
            and condEnum not in condTypeExclusions[condType]
        ):
            labelDict[condEnum.value] = GetAutoConditionLabel(condEnum)
    return labelDict

def GetClauseTextInputs(clauseEnum: TreatyClauses) -> dict[str,discord.ui.TextInput]:
    textInputs: dict[str,discord.ui.TextInput] = {}
    match(clauseEnum):
        case TreatyClauses.CEDE_LAND:
            textInputs["losingCountry"] = discord.ui.TextInput(
                label = "Full name of country that is giving land",
                style = discord.TextStyle.short,
                placeholder = "Enter country name...",
                required = True)
            textInputs["gainingCountry"] = discord.ui.TextInput(
                label = "Full name of country that is recieving land",
                style = discord.TextStyle.short,
                placeholder = "Enter country name...",
                required = True)
            textInputs["lostStates"] = discord.ui.TextInput(
                label = "List of taken states",
                style = discord.TextStyle.long,
                placeholder = "Enter comma-separated state names...",
                required = True)
        case TreatyClauses.CEASEFIRE:
            textInputs["ceasefireCountries"] = discord.ui.TextInput(
                label = "List of countries ceasing hostilities",
                style = discord.TextStyle.long,
                placeholder = "Enter comma-separated country names...\n(Put 'all' for all signatories)",
                required = True)
        case TreatyClauses.FORMAL_PEACE:
            textInputs["peaceCountries"] = discord.ui.TextInput(
                label = "List of countries declaring peace",
                style = discord.TextStyle.long,
                placeholder = "Enter comma-separated country names...\n(Put 'all' for all signatories)",
                required = True)
        case TreatyClauses.NON_AGGRESSION:
            textInputs["nonAggressionCountries"] = discord.ui.TextInput(
                label = "List of countries agreeing to non-aggression",
                style = discord.TextStyle.long,
                placeholder = "Enter comma-separated country names...\n(Put 'all' for all signatories)",
                required = True)
        case TreatyClauses.MUTUAL_DEFENSE:
            textInputs["defensiveCountries"] = discord.ui.TextInput(
                label = "List of countries agreeing to mutual defense",
                style = discord.TextStyle.long,
                placeholder = "Enter comma-separated country names...\n(Put 'all' for all signatories)",
                required = True)
        case TreatyClauses.DEMILITARISE_ZONE:
            textInputs["demilCountry"] = discord.ui.TextInput(
                label = "Name of country that is demilitarising states",
                style = discord.TextStyle.short,
                placeholder = "Enter country name...",
                required = True)
            textInputs["demilStates"] = discord.ui.TextInput(
                label = "List of demilitarised states",
                style = discord.TextStyle.long,
                placeholder = "Enter comma-separated state names...",
                required = True)
        case TreatyClauses.MILITARY_ACCESS:
            textInputs["accessingCountries"] = discord.ui.TextInput(
                label = "List of countries recieving military access",
                style = discord.TextStyle.long,
                placeholder = "Enter comma-separated country names...\n(Put 'all' for all signatories)",
                required = True)
            textInputs["accessedCountries"] = discord.ui.TextInput(
                label = "List of countries giving military access",
                style = discord.TextStyle.long,
                placeholder = "Enter comma-separated country names...\n(Put 'all' for all signatories)",
                required = True)
        case TreatyClauses.GUARANTEE_INDEPENDENCE:
            textInputs["guarantorCountries"] = discord.ui.TextInput(
                label = "List of countries who are guaranteeing",
                style = discord.TextStyle.long,
                placeholder = "Enter comma-separated country names...\n(Put 'all' for all signatories)",
                required = True)
            textInputs["guaranteedCountries"] = discord.ui.TextInput(
                label = "List of countries being guaranteed",
                style = discord.TextStyle.long,
                placeholder = "Enter comma-separated country names...\n(Put 'all' for all signatories)",
                required = True)
        case TreatyClauses.DECLARE_WAR:
            textInputs["alliedCountries"] = discord.ui.TextInput(
                label = "List of countries declaring war",
                style = discord.TextStyle.long,
                placeholder = "Enter comma-separated country names...\n(Put 'all' for all signatories)",
                required = True)
            textInputs["opposingCountries"] = discord.ui.TextInput(
                label = "List of countries being declared on",
                style = discord.TextStyle.long,
                placeholder = "Enter comma-separated country names...",
                required = True)
        case TreatyClauses.PAY_MONEY:
            textInputs["givingCountry"] = discord.ui.TextInput(
                label = "Full name of country giving money",
                style = discord.TextStyle.short,
                placeholder = "Enter country name...",
                required = True
            )
            textInputs["receivingCountry"] = discord.ui.TextInput(
                label = "Full name of country receiving money",
                style = discord.TextStyle.short,
                placeholder = "Enter country name...",
                required = True
            )
            textInputs["moneyGiven"] = discord.ui.TextInput(
                label = "Amount of cash given",
                style = discord.TextStyle.short,
                placeholder = "Enter positive number...",
                required = True
            )
            textInputs["yearFrequency"] = discord.ui.TextInput(
                label = "Year frequency of money given (Optional)",
                style = discord.TextStyle.short,
                placeholder = "Enter an integer...",
                required = False
            )
        case TreatyClauses.EMBARGO:
            textInputs["embargoingCountries"] = discord.ui.TextInput(
                label = "List of countries embargoing",
                style = discord.TextStyle.long,
                placeholder = "Enter comma-separated country names...\n(Put 'all' for all signatories)",
                required = True)
            textInputs["embargoedCountries"] = discord.ui.TextInput(
                label = "List of countries being embargoed",
                style = discord.TextStyle.long,
                placeholder = "Enter comma-separated country names...",
                required = True)
        case _:
            raise ValueError(f"no case is defined for {clauseEnum} in GetTreatyClauseTextInputs()")
    return textInputs

def GetAutoConditionTextInputs(condEnum: TreatyConditions) -> dict[str,discord.ui.TextInput]:
    textInputs: dict[str,discord.ui.TextInput] = {}
    match(condEnum):
        case TreatyConditions.AFTER_TIME_EIF:
            textInputs["yearNo"] = discord.ui.TextInput(
                label = "Number of years after Entry Into Force",
                style = discord.TextStyle.short,
                placeholder = "Enter Integer...",
                required = True)
        case TreatyConditions.BEFORE_TIME_EIF:
            textInputs["yearNo"] = discord.ui.TextInput(
                label = "Number of years after Entry Into Force",
                style = discord.TextStyle.short,
                placeholder = "Enter Integer...",
                required = True)
        case TreatyConditions.AFTER_DATE:
            textInputs["afterDate"] = discord.ui.TextInput(
                label = "Date after which condition is true",
                style = discord.TextStyle.short,
                placeholder = "Enter Date in DD/MM/YYYY Format...",
                required = True)
        case TreatyConditions.BEFORE_DATE:
            textInputs["beforeDate"] = discord.ui.TextInput(
                label = "Date before which condition is true",
                style = discord.TextStyle.short,
                placeholder = "Enter Date in DD/MM/YYYY Format...",
                required = True)
        case TreatyConditions.SIGNATORIES_INCLUDED:
            textInputs["signatoryCountries"] = discord.ui.TextInput(
                label = "List of countries who need to be signatories",
                style = discord.TextStyle.long,
                placeholder = "Enter comma-separated country names...",
                required = True)
        case TreatyConditions.SIGNATORIES_NO:
            textInputs["inequality"] = discord.ui.TextInput(
                label = "At least X or at most X",
                style = discord.TextStyle.short,
                placeholder = "Enter 'at least' or 'at most'",
                required = True)
            textInputs["signatoriesNo"] = discord.ui.TextInput(
                label = "Number of signatories X",
                style = discord.TextStyle.short,
                placeholder = "Enter Integer...",
                required = True)
        case TreatyConditions.AT_WAR_WITH:
            textInputs["isNegative"] = discord.ui.TextInput(
                label = "Condition true if below false?",
                style = discord.TextStyle.short,
                placeholder = "Enter 'yes' or 'no'",
                required = True)
            textInputs["warCountries"] = discord.ui.TextInput(
                label = "List of countries who need to be at war with",
                style = discord.TextStyle.long,
                placeholder = "Enter comma-separated country names...",
                required = True)
        case TreatyConditions.OTHER_TREATY_MEMBER:
            textInputs["isNegative"] = discord.ui.TextInput(
                label = "Condition true if below false?",
                style = discord.TextStyle.short,
                placeholder = "Enter 'yes' or 'no'",
                required = True)
            textInputs["otherTreaty"] = discord.ui.TextInput(
                label = "Full name of treaty needed to be in",
                style = discord.TextStyle.short,
                placeholder = "Enter country name...",
                required = True)
        case TreatyConditions.IN_COUNTRY_LIST:
            textInputs["isNegative"] = discord.ui.TextInput(
                label = "Condition true if below false?",
                style = discord.TextStyle.short,
                placeholder = "Enter 'yes' or 'no'",
                required = True)
            textInputs["possibleCountries"] = discord.ui.TextInput(
                label = "List of countries who they need to be one of",
                style = discord.TextStyle.long,
                placeholder = "Enter comma-separated country names...",
                required = True)
        case _:
            raise ValueError(f"no case is defined for {condEnum} in GetTreatyConditionTextInputs()")
    return textInputs

def GetClauseArgs(cursor: sqlite3.Cursor, clauseEnum: TreatyClauses, textInputs: dict[str,discord.ui.TextInput]) -> dict[str,Any]:
    class ClauseArgsHelper:
        @staticmethod
        def GetCountryIDList(cursor: sqlite3.Cursor, countryNameList: list[str]) -> list[int]:
            cursor.execute("""
                SELECT nation_id, name
                FROM playernations
            """)
            countryRows = cursor.fetchall()
            allCountryNameList = [countryRow[1] for countryRow in countryRows]

            countryIDList = []
            for countryName in countryNameList:
                close_matches = get_close_matches(countryName, allCountryNameList, n=1)
                if not close_matches: raise ValueError(f"no match found for country {countryName}")
                countryIDList.append(countryRows[allCountryNameList.index(close_matches[0])][0])
            
            return countryIDList

        @staticmethod
        def GetCountryID(cursor: sqlite3.Cursor, countryName: str) -> int:
            cursor.execute("""
                SELECT nation_id, name
                FROM playernations
            """)
            countryRows = cursor.fetchall()
            allCountryNameList = [countryRow[1] for countryRow in countryRows]

            close_matches = get_close_matches(countryName, allCountryNameList, n=1)
            if not close_matches: raise ValueError(f"no match found for country {countryName}")
            countryID = countryRows[allCountryNameList.index(close_matches[0])][0]

            return countryID

        @staticmethod
        def GetStateIDList(cursor: sqlite3.Cursor, stateNameList: list[str], parentCountryID: int | None = None) -> list[int]:
            cursor.execute("""
                SELECT state_id, name, nation_id
                FROM states
            """)
            stateRows = cursor.fetchall()
            allStateNameList = [stateRow[1] for stateRow in stateRows]

            stateIDList = []
            for stateName in stateNameList:
                close_matches = get_close_matches(stateName, allStateNameList, n=1)
                if not close_matches: raise ValueError(f"no match found for state {stateName}")
                stateIndex = allStateNameList.index(close_matches[0])
                if parentCountryID is not None and stateRows[stateIndex][2] != parentCountryID: raise ValueError(f"state {stateName} does not belong to specified country (id {parentCountryID})")
                stateIDList.append(stateRows[stateIndex][0])
            
            return stateIDList
    
    clauseArgs: dict[str,Any] = {}
    match(clauseEnum):
        case TreatyClauses.CEDE_LAND:
            losingCountryName = textInputs["losingCountry"].value
            gainingCountryName = textInputs["gainingCountry"].value
            lostStatesString = textInputs["lostStates"].value

            lostStateNames = lostStatesString.split(',')
            if len(lostStateNames) == 0: raise ValueError(f"no state names given")
            
            losingCountryID = ClauseArgsHelper.GetCountryID(cursor, losingCountryName)
            gainingCountryID = ClauseArgsHelper.GetCountryID(cursor, gainingCountryName)
            if gainingCountryID == losingCountryID: raise ValueError(f"country ID for {losingCountryName} and {gainingCountryName} is the same")
            lostStateIDs = ClauseArgsHelper.GetStateIDList(cursor, lostStateNames, losingCountryID)
            
            clauseArgs["losingCountryID"] = losingCountryID
            clauseArgs["lostStateIDs"] = lostStateIDs
            clauseArgs["gainingCountryID"] = gainingCountryID
        case TreatyClauses.CEASEFIRE:
            ceasefireCountriesString = textInputs["ceasefireCountries"].value

            ceasefireCountryIDs: str | list[int] = []
            if ceasefireCountriesString == 'all':
                ceasefireCountryIDs = 'all'
            else:
                ceasefireCountryNames = ceasefireCountriesString.split(',')
                if len(ceasefireCountryNames) == 0: raise ValueError(f"no country names given")
                ceasefireCountryIDs = ClauseArgsHelper.GetCountryIDList(cursor, ceasefireCountryNames)

            clauseArgs["ceasefireCountryIDs"] = ceasefireCountryIDs
        case TreatyClauses.FORMAL_PEACE:
            peaceCountriesString = textInputs["peaceCountries"].value

            peaceCountryIDs: str | list[int] = []
            if peaceCountriesString == 'all':
                peaceCountryIDs = 'all'
            else:
                peaceCountryNames = peaceCountriesString.split(',')
                if len(peaceCountryNames) == 0: raise ValueError(f"no country names given")
                peaceCountryIDs = ClauseArgsHelper.GetCountryIDList(cursor, peaceCountryNames)

            clauseArgs["peaceCountryIDs"] = peaceCountryIDs
        case TreatyClauses.NON_AGGRESSION:
            nonAggressionCountriesString = textInputs["nonAggressionCountries"].value

            nonAggressionCountryIDs: str | list[int] = []
            if nonAggressionCountriesString == 'all':
                nonAggressionCountryIDs = 'all'
            else:
                nonAggressionCountryNames = nonAggressionCountriesString.split(',')
                if len(nonAggressionCountryNames) == 0: raise ValueError(f"no country names given")
                nonAggressionCountryIDs = ClauseArgsHelper.GetCountryIDList(cursor, nonAggressionCountryNames)

            clauseArgs["nonAggressionCountryIDs"] = nonAggressionCountryIDs
        case TreatyClauses.MUTUAL_DEFENSE:
            defensiveCountriesString = textInputs["defensiveCountries"].value

            defensiveCountryIDs: str | list[int]
            if defensiveCountriesString == 'all':
                defensiveCountryIDs = 'all'
            else:
                defensiveCountryNames = defensiveCountriesString.split(',')
                if len(defensiveCountryNames) == 0: raise ValueError(f"no country names given")
                defensiveCountryIDs = ClauseArgsHelper.GetCountryIDList(cursor, defensiveCountryNames)

            clauseArgs["defensiveCountryIDs"] = defensiveCountryIDs
        case TreatyClauses.DEMILITARISE_ZONE:
            demilCountryName = textInputs["demilCountry"].value
            demilStatesString = textInputs["demilStates"].value

            demilCountryID = ClauseArgsHelper.GetCountryID(cursor, demilCountryName)

            stateNames = demilStatesString.split(',')
            if len(stateNames) == 0: raise ValueError(f"no state names given")
            
            demilStateIDs = ClauseArgsHelper.GetStateIDList(cursor, stateNames, demilCountryID)
            
            clauseArgs["demilCountryID"] = demilCountryID
            clauseArgs["demilStateIDs"] = demilStateIDs
        case TreatyClauses.MILITARY_ACCESS:
            accessingCountriesString = textInputs["accessingCountries"].value
            accessedCountriesString = textInputs["accessedCountries"].value

            accessingCountryIDs: str | list[int]
            if accessingCountriesString == 'all':
                accessingCountryIDs = 'all'
            else:
                accessingCountryNames = accessingCountriesString.split(',')
                if len(accessingCountryNames) == 0: raise ValueError(f"no accessing country names given")
                accessingCountryIDs = ClauseArgsHelper.GetCountryIDList(cursor, accessingCountryNames)

            accessedCountryIDs: str | list[int]
            if accessedCountriesString == 'all':
                accessedCountryIDs = 'all'
            else:
                accessedCountryNames = accessedCountriesString.split(',')
                if len(accessedCountryNames) == 0: raise ValueError(f"no guaranteed country names given")
                accessedCountryIDs = ClauseArgsHelper.GetCountryIDList(cursor, accessedCountryNames)

            clauseArgs["accessingCountryIDs"] = accessingCountryIDs
            clauseArgs["accessedCountryIDs"] = accessedCountryIDs
        case TreatyClauses.GUARANTEE_INDEPENDENCE:
            guarantorCountriesString = textInputs["guarantorCountries"].value
            guaranteedCountriesString = textInputs["guaranteedCountries"].value

            guarantorCountryIDs: str | list[int]
            if guarantorCountriesString == 'all':
                guarantorCountryIDs = 'all'
            else:
                guarantorCountryNames = guarantorCountriesString.split(',')
                if len(guarantorCountryNames) == 0: raise ValueError(f"no guarantor country names given")
                guarantorCountryIDs = ClauseArgsHelper.GetCountryIDList(cursor, guarantorCountryNames)

            guaranteedCountryIDs: str | list[int]
            if guaranteedCountriesString == 'all':
                guaranteedCountryIDs = 'all'
            else:
                guaranteedCountryNames = guaranteedCountriesString.split(',')
                if len(guaranteedCountryNames) == 0: raise ValueError(f"no guaranteed country names given")
                guaranteedCountryIDs = ClauseArgsHelper.GetCountryIDList(cursor, guaranteedCountryNames)

            clauseArgs["guarantorCountryIDs"] = guarantorCountryIDs
            clauseArgs["guaranteedCountryIDs"] = guaranteedCountryIDs
        case TreatyClauses.DECLARE_WAR:
            alliedCountriesString = textInputs["alliedCountries"].value
            opposingCountriesString = textInputs["opposingCountries"].value

            alliedCountryIDs: str | list[int]
            if alliedCountriesString == 'all':
                alliedCountryIDs = 'all'
            else:
                alliedCountryNames = alliedCountriesString.split(',')
                if len(alliedCountryNames) == 0: raise ValueError(f"no allied country names given")
                alliedCountryIDs = ClauseArgsHelper.GetCountryIDList(cursor, alliedCountryNames)
            
            opposingCountryNames = opposingCountriesString.split(',')
            if len(opposingCountryNames) == 0: raise ValueError(f"no opposing country names given")
            opposingCountryIDs = ClauseArgsHelper.GetCountryIDList(cursor, opposingCountryNames)

            clauseArgs["alliedCountryIDs"] = alliedCountryIDs
            clauseArgs["opposingCountryIDs"] = opposingCountryIDs
        case TreatyClauses.PAY_MONEY:
            givingCountryName = textInputs["givingCountry"].value
            receivingCountryName = textInputs["receivingCountry"].value
            moneyGivenStr = textInputs["moneyGiven"].value
            yearFrequencyStr = textInputs["yearFrequency"].value
            
            givingCountryID = ClauseArgsHelper.GetCountryID(cursor, givingCountryName)
            receivingCountryID = ClauseArgsHelper.GetCountryID(cursor, receivingCountryName)
            if receivingCountryID == givingCountryID: raise ValueError(f"country ID for {givingCountryName} and {receivingCountryName} is the same")

            try: moneyGiven = float(moneyGivenStr)
            except: raise ValueError("moneyGiven must be a valid decimal")
            if moneyGiven <= 0: raise ValueError("moneyGiven must be positive")
            moneyGiven = round(moneyGiven, 2)

            if yearFrequencyStr is None:
                yearFrequency = None
            else:
                try: yearFrequency = int(yearFrequencyStr)
                except: raise ValueError("yearFrequency must be an integer")
                if yearFrequency <= 0: raise ValueError("yearFrequency must be positive")
            
            clauseArgs["givingCountryID"] = givingCountryID
            clauseArgs["receivingCountryID"] = receivingCountryID
            clauseArgs["moneyGiven"] = moneyGiven
            clauseArgs["yearFrequency"] = yearFrequency
        case TreatyClauses.EMBARGO:
            embargoingCountriesString = textInputs["embargoingCountries"].value
            embargoedCountriesString = textInputs["embargoedCountries"].value

            embargoingCountryIDs: str | list[int]
            if embargoingCountriesString == 'all':
                embargoingCountryIDs = 'all'
            else:
                embargoingCountryNames = embargoingCountriesString.split(',')
                if len(embargoingCountryNames) == 0: raise ValueError(f"no embargoing country names given")
                embargoingCountryIDs = ClauseArgsHelper.GetCountryIDList(cursor, embargoingCountryNames)

            embargoedCountryNames = embargoedCountriesString.split(',')
            if len(embargoedCountryNames) == 0: raise ValueError(f"no embargoed country names given")
            embargoedCountryIDs = ClauseArgsHelper.GetCountryIDList(cursor, embargoedCountryNames)

            clauseArgs["embargoingCountryIDs"] = embargoingCountryIDs
            clauseArgs["embargoedCountryIDs"] = embargoedCountryIDs
        case _:
            raise ValueError(f"no case is defined for {clauseEnum} in GetTreatyClauseArgs()")
    
    return clauseArgs

def GetAutoConditionArgs(cursor: sqlite3.Cursor, condEnum: TreatyConditions, textInputs: dict[str,discord.ui.TextInput]) -> dict[str,Any]:
    class CondArgsHelper:
        @staticmethod
        def GetCountryIDList(cursor: sqlite3.Cursor, countryNameList: list[str]) -> list[int]:
            cursor.execute("""
                SELECT nation_id, name
                FROM playernations
            """)
            countryRows = cursor.fetchall()
            allCountryNameList = [countryRow[1] for countryRow in countryRows]

            countryIDList = []
            for countryName in countryNameList:
                close_matches = get_close_matches(countryName, allCountryNameList, n=1)
                if not close_matches: raise ValueError(f"no match found for country {countryName}")
                countryIDList.append(countryRows[allCountryNameList.index(close_matches[0])][0])
            
            return countryIDList

        @staticmethod
        def GetCountryID(cursor: sqlite3.Cursor, countryName: str) -> int:
            cursor.execute("""
                SELECT nation_id, name
                FROM playernations
            """)
            countryRows = cursor.fetchall()
            allCountryNameList = [countryRow[1] for countryRow in countryRows]

            close_matches = get_close_matches(countryName, allCountryNameList, n=1)
            if not close_matches: raise ValueError(f"no match found for country {countryName}")
            countryID = countryRows[allCountryNameList.index(close_matches[0])][0]

            return countryID
        
        @staticmethod
        def GetTreatyID(cursor: sqlite3.Cursor, treatyName: str) -> int:
            cursor.execute("""
                SELECT treaty_id, treaty_name
                FROM treaties
            """)
            treatyRows = cursor.fetchall()
            allTreatyNameList = [treatyRow[1] for treatyRow in treatyRows]

            close_matches = get_close_matches(treatyName, allTreatyNameList, n=1)
            if not close_matches: raise ValueError(f"no match found for treaty {treatyName}")
            treatyID = treatyRows[allTreatyNameList.index(close_matches[0])][0]

            return treatyID

        @staticmethod
        def GetStateIDList(cursor: sqlite3.Cursor, stateNameList: list[str], parentCountryID: int | None = None) -> list[int]:
            cursor.execute("""
                SELECT state_id, name, nation_id
                FROM states
            """)
            stateRows = cursor.fetchall()
            allStateNameList = [stateRow[1] for stateRow in stateRows]

            stateIDList = []
            for stateName in stateNameList:
                close_matches = get_close_matches(stateName, allStateNameList, n=1)
                if not close_matches: raise ValueError(f"no match found for state {stateName}")
                stateIndex = allStateNameList.index(close_matches[0])
                if parentCountryID is not None and stateRows[stateIndex][2] != parentCountryID: raise ValueError(f"state {stateName} does not belong to specified country (id {parentCountryID})")
                stateIDList.append(stateRows[stateIndex][0])
            
            return stateIDList
    
    condArgs: dict[str,Any] = {}

    match(condEnum):
        case TreatyConditions.AFTER_TIME_EIF:
            yearNoStr = textInputs["yearNo"].value

            if not yearNoStr.isdigit(): raise ValueError("year no given is not a positive integer")
            yearNo = int(yearNoStr)

            condArgs["yearNo"] = yearNo
        case TreatyConditions.BEFORE_TIME_EIF:
            yearNoStr = textInputs["yearNo"].value

            if not yearNoStr.isdigit(): raise ValueError("year no given is not a positive integer")
            yearNo = int(yearNoStr)

            condArgs["yearNo"] = yearNo
        case TreatyConditions.AFTER_DATE:
            afterDateStr = textInputs["afterDate"].value

            try: GetDateValue(afterDateStr)
            except: raise ValueError("date given is invalid")

            condArgs["afterDate"] = afterDateStr
        case TreatyConditions.BEFORE_DATE:
            beforeDateStr = textInputs["beforeDate"].value

            try: GetDateValue(beforeDateStr)
            except: raise ValueError("date given is invalid")

            condArgs["beforeDate"] = beforeDateStr
        case TreatyConditions.SIGNATORIES_INCLUDED:
            signatoryCountriesString = textInputs["signatoryCountries"].value

            signatoryCountryNames = signatoryCountriesString.split(',')
            if len(signatoryCountryNames) == 0: raise ValueError(f"no country names given")
            signatoryCountryIDs = CondArgsHelper.GetCountryIDList(cursor, signatoryCountryNames)

            condArgs["signatoryCountryIDs"] = signatoryCountryIDs
        case TreatyConditions.SIGNATORIES_NO:
            inequality = textInputs["inequality"].value
            signatoriesNoStr = textInputs["signatoriesNo"].value

            if inequality not in ["at least","at most"]: raise ValueError("first argument must be 'at least' or 'at most'")
            if not signatoriesNoStr.isdigit(): raise ValueError("signatories no given is not a positive integer")
            signatoriesNo = int(signatoriesNoStr)

            condArgs["inequality"] = inequality
            condArgs["signatoriesNo"] = signatoriesNo
        case TreatyConditions.AT_WAR_WITH:
            isNegativeStance = textInputs["isNegative"].value
            warCountriesString = textInputs["warCountries"].value

            warCountryNames = warCountriesString.split(',')
            if len(warCountryNames) == 0: raise ValueError(f"no country names given")
            warCountryIDs = CondArgsHelper.GetCountryIDList(cursor, warCountryNames)

            if isNegativeStance == "yes": isNegative = True
            elif isNegativeStance == "no": isNegative = False
            else: raise ValueError("isNegative must be 'yes' or 'no'")

            condArgs["warCountryIDs"] = warCountryIDs
            condArgs["isNegative"] = isNegative
        case TreatyConditions.OTHER_TREATY_MEMBER:
            isNegativeStance = textInputs["isNegative"].value
            otherTreatyName = textInputs["otherTreaty"].value

            otherTreatyID = CondArgsHelper.GetTreatyID(cursor, otherTreatyName)

            if isNegativeStance == "yes": isNegative = True
            elif isNegativeStance == "no": isNegative = False
            else: raise ValueError("isNegative must be 'yes' or 'no'")

            condArgs["otherTreatyID"] = otherTreatyID
            condArgs["isNegative"] = isNegative
        case TreatyConditions.IN_COUNTRY_LIST:
            isNegativeStance = textInputs["isNegative"].value
            possibleCountriesString = textInputs["possibleCountries"].value

            possibleCountryNames = possibleCountriesString.split(',')
            if len(possibleCountryNames) == 0: raise ValueError(f"no country names given")
            possibleCountryIDs = CondArgsHelper.GetCountryIDList(cursor, possibleCountryNames)

            if isNegativeStance == "yes": isNegative = True
            elif isNegativeStance == "no": isNegative = False
            else: raise ValueError("isNegative must be 'yes' or 'no'")

            condArgs["possibleCountryIDs"] = possibleCountryIDs
            condArgs["isNegative"] = isNegative
        case _:
            raise ValueError(f"no case is defined for {condEnum} in GetTreatyConditionArgs()")

    return condArgs

def GetVoteArgs(cursor: sqlite3.Cursor, textInputs: dict[str,discord.ui.TextInput]) -> dict[str,Any]:
    class VoteArgsHelper:
        @staticmethod
        def GetCountryIDList(cursor: sqlite3.Cursor, countryNameList: list[str]) -> list[int]:
            cursor.execute("""
                SELECT nation_id, name
                FROM playernations
            """)
            countryRows = cursor.fetchall()
            allCountryNameList = [countryRow[1] for countryRow in countryRows]

            countryIDList = []
            for countryName in countryNameList:
                close_matches = get_close_matches(countryName, allCountryNameList, n=1)
                if not close_matches: raise ValueError(f"no match found for country {countryName}")
                countryIDList.append(countryRows[allCountryNameList.index(close_matches[0])][0])
            
            return countryIDList

        @staticmethod
        def GetCountryID(cursor: sqlite3.Cursor, countryName: str) -> int:
            cursor.execute("""
                SELECT nation_id, name
                FROM playernations
            """)
            countryRows = cursor.fetchall()
            allCountryNameList = [countryRow[1] for countryRow in countryRows]

            close_matches = get_close_matches(countryName, allCountryNameList, n=1)
            if not close_matches: raise ValueError(f"no match found for country {countryName}")
            countryID = countryRows[allCountryNameList.index(close_matches[0])][0]

            return countryID

        @staticmethod
        def GetStateIDList(cursor: sqlite3.Cursor, stateNameList: list[str], parentCountryID: int | None = None) -> list[int]:
            cursor.execute("""
                SELECT state_id, name, nation_id
                FROM states
            """)
            stateRows = cursor.fetchall()
            allStateNameList = [stateRow[1] for stateRow in stateRows]

            stateIDList = []
            for stateName in stateNameList:
                close_matches = get_close_matches(stateName, allStateNameList, n=1)
                if not close_matches: raise ValueError(f"no match found for state {stateName}")
                stateIndex = allStateNameList.index(close_matches[0])
                if parentCountryID is not None and stateRows[stateIndex][2] != parentCountryID: raise ValueError(f"state {stateName} does not belong to specified country (id {parentCountryID})")
                stateIDList.append(stateRows[stateIndex][0])
            
            return stateIDList
    
    voteArgs: dict[str,Any] = {}

    requiredPercentageStr = textInputs["requiredPercentage"].value
    participantCountriesStr = textInputs["participantCountries"].value
    vetoCountriesStr = textInputs["vetoCountries"].value
    callCountriesStr = textInputs["callCountries"].value

    if not requiredPercentageStr.isdigit(): raise ValueError("required percentage needs to be a positive integer")
    requiredPercentage = int(requiredPercentageStr)
    if requiredPercentage < 0 or requiredPercentage > 100: raise ValueError("percentage must be from 0-100")

    participantCountryIDs: str | list[int]
    if participantCountriesStr == "all":
        participantCountryIDs = "all"
    else:
        participantCountryNames = participantCountriesStr.split()
        if len(participantCountryNames) == 0: 
            participantCountryIDs = []
        else:
            participantCountryIDs = VoteArgsHelper.GetCountryIDList(cursor = cursor, countryNameList = participantCountryNames)
    
    vetoCountryIDs: str | list[int]
    if vetoCountriesStr == "all":
        vetoCountryIDs = "all"
    else:
        vetoCountryNames = vetoCountriesStr.split()
        if len(vetoCountryNames) == 0: 
            vetoCountryIDs = []
        else:
            vetoCountryIDs = VoteArgsHelper.GetCountryIDList(cursor = cursor, countryNameList = vetoCountryNames)
    
    callCountryIDs: str | list[int]
    if callCountriesStr == "all":
        callCountryIDs = "all"
    else:
        callCountryNames = callCountriesStr.split()
        if len(callCountryNames) == 0: 
            callCountryIDs = []
        else:
            callCountryIDs = VoteArgsHelper.GetCountryIDList(cursor = cursor, countryNameList = callCountryNames)

    voteArgs["requiredPercentage"] = requiredPercentage
    voteArgs["participantCountryIDs"] = participantCountryIDs
    voteArgs["vetoCountryIDs"] = vetoCountryIDs
    voteArgs["callCountryIDs"] = callCountryIDs
    
    return voteArgs

def AddClauseToDB(cursor: sqlite3.Cursor, treatyID: int, clauseEnum: TreatyClauses, clauseArgs: dict[str,Any]) -> None:
    cursor.execute("""
        SELECT treaty_args
        FROM treaties
        WHERE treaty_id == (?)
    """, (treatyID,))
    row = cursor.fetchone()
    if row is None: raise ValueError(f"no treaty with ID {treatyID} found")
    treatyArgs = row[0]
    
    if treatyArgs is None: raise ValueError(f"treaty_args column for treaty with ID {treatyID} is null")
    treatyArgs = json.loads(treatyArgs)
    if treatyArgs.get("clauses") is None: treatyArgs["clauses"] = []
    clauses = treatyArgs["clauses"]
    clauses.append({"clauseEnum": clauseEnum.value, "clauseArgs": clauseArgs})
    treatyArgs["clauses"] = clauses
    treatyArgs = json.dumps(treatyArgs)

    cursor.execute("""
        UPDATE treaties
        SET treaty_args = (?)
        WHERE treaty_id == (?)
    """, (treatyArgs, treatyID))

def AddAutoConditionBlockToDB(cursor: sqlite3.Cursor, treatyID: int, condType: str) -> int:
    cursor.execute("""
        SELECT treaty_args
        FROM treaties
        WHERE treaty_id == (?)
    """, (treatyID,))
    row = cursor.fetchone()
    if row is None: raise ValueError(f"no treaty with ID {treatyID} found")
    treatyArgs = row[0]
    
    if treatyArgs is None: raise ValueError(f"treaty_args column for treaty with ID {treatyID} is null")
    treatyArgs = json.loads(treatyArgs)
    if treatyArgs.get(condType) is None: treatyArgs[condType] = []
    condBlocks = treatyArgs[condType]
    condBlocks.append({"blockType": "auto", "conditions": []})
    blockIndex = len(condBlocks) - 1
    
    treatyArgs = json.dumps(treatyArgs)

    cursor.execute("""
        UPDATE treaties
        SET treaty_args = (?)
        WHERE treaty_id == (?)
    """, (treatyArgs, treatyID))

    return blockIndex

def AddVoteConditionBlockToDB(cursor: sqlite3.Cursor, treatyID: int, condType: str, voteArgs: dict[str,Any]) -> int:
    cursor.execute("""
        SELECT treaty_args
        FROM treaties
        WHERE treaty_id == (?)
    """, (treatyID,))
    row = cursor.fetchone()
    if row is None: raise ValueError(f"no treaty with ID {treatyID} found")
    treatyArgs = row[0]
    
    if treatyArgs is None: raise ValueError(f"treaty_args column for treaty with ID {treatyID} is null")
    treatyArgs = json.loads(treatyArgs)
    if treatyArgs.get(condType) is None: treatyArgs[condType] = []
    condBlocks = treatyArgs[condType]
    condBlocks.append({"blockType": "vote", "voteArgs": voteArgs, "conditions": []})
    blockIndex = len(condBlocks) - 1
    
    treatyArgs = json.dumps(treatyArgs)

    cursor.execute("""
        UPDATE treaties
        SET treaty_args = (?)
        WHERE treaty_id == (?)
    """, (treatyArgs, treatyID))

    return blockIndex

def AddAutoConditionToDB(cursor: sqlite3.Cursor, treatyID: int, condType: str, condEnum: TreatyConditions, condArgs: dict[str,Any], blockIndex: int) -> None:
    cursor.execute("""
        SELECT treaty_args
        FROM treaties
        WHERE treaty_id == (?)
    """, (treatyID,))
    row = cursor.fetchone()
    if row is None: raise ValueError(f"no treaty with ID {treatyID} found")
    treatyArgs = row[0]
    
    if treatyArgs is None: raise ValueError(f"treaty_args column for treaty with ID {treatyID} is null")
    treatyArgs = json.loads(treatyArgs)
    if condType == "clause":
        if treatyArgs.get("clauses") is None: treatyArgs["clauses"] = []
        clauses = treatyArgs["clauses"]
        if len(clauses) <= blockIndex: raise ValueError("block index given does not exist")
        clause = clauses[blockIndex]
        if clause.get("conditions") is None: clause["conditions"] = []
        clause["conditions"].append({"condEnum": condEnum.value, "condArgs": condArgs})
        treatyArgs["clauses"] = clauses
    else:
        if treatyArgs.get(condType) is None: treatyArgs[condType] = []
        condBlocks = treatyArgs[condType]
        if len(condBlocks) <= blockIndex: raise ValueError("block index given does not exist")
        condBlock = condBlocks[blockIndex]
        if condBlock["blockType"] != "auto": raise ValueError(f"cannot add condition to block of type{condBlock["blockType"]}")
        if condBlock.get("conditions") is None: condBlock["conditions"] = []
        conditions = condBlock["conditions"]
        conditions.append({"condEnum": condEnum.value, "condArgs": condArgs})

    treatyArgs = json.dumps(treatyArgs)

    cursor.execute("""
        UPDATE treaties
        SET treaty_args = (?)
        WHERE treaty_id == (?)
    """, (treatyArgs, treatyID))

def AddVoteConditionToDB(cursor: sqlite3.Cursor, treatyID: int, condType: str, condString: str, blockIndex: int) -> None:
    cursor.execute("""
        SELECT treaty_args
        FROM treaties
        WHERE treaty_id == (?)
    """, (treatyID,))
    row = cursor.fetchone()
    if row is None: raise ValueError(f"no treaty with ID {treatyID} found")
    treatyArgs = row[0]
    
    if treatyArgs is None: raise ValueError(f"treaty_args column for treaty with ID {treatyID} is null")
    treatyArgs = json.loads(treatyArgs)
    if condType == "clause":
        raise ValueError("condType cannot be clause for a vote condition")
    else:
        if treatyArgs.get(condType) is None: treatyArgs[condType] = []
        condBlocks = treatyArgs[condType]
        if len(condBlocks) <= blockIndex: raise ValueError("block index given does not exist")
        condBlock = condBlocks[blockIndex]
        if condBlock["blockType"] != "vote": raise ValueError(f"cannot add condition to block of type{condBlock["blockType"]}")
        if condBlock.get("conditions") is None: condBlock["conditions"] = []
        conditions = condBlock["conditions"]
        conditions.append(condString)

    treatyArgs = json.dumps(treatyArgs)

    cursor.execute("""
        UPDATE treaties
        SET treaty_args = (?)
        WHERE treaty_id == (?)
    """, (treatyArgs, treatyID))

def AddSignatoryToDB(cursor: sqlite3.Cursor, treatyID: int, countryID: int) -> None:
    cursor.execute("""
        SELECT signed_treaties
        FROM playernations
        WHERE nation_id == (?)
    """, (countryID,))
    row = cursor.fetchone()
    if row is None: raise ValueError(f"no country with ID {countryID} found")
    treatyInfoList = row[0]
    
    if treatyInfoList is None: raise ValueError(f"signed_treaties column for country with ID {countryID} is null")
    treatyInfoList = json.loads(treatyInfoList)
    
    treatyInfoList.append({"treatyID": treatyID, "dateSigned": GetCurrentGameDateString()})

    treatyInfoList = json.dumps(treatyInfoList)

    cursor.execute("""
        UPDATE playernations
        SET signed_treaties = (?)
        WHERE nation_id == (?)
    """, (treatyInfoList, countryID))

def DeleteClauseFromDB(cursor: sqlite3.Cursor, treatyID: int, clauseIndex: int) -> None:
    cursor.execute("""
        SELECT treaty_args
        FROM treaties
        WHERE treaty_id == (?)
    """, (treatyID,))
    row = cursor.fetchone()
    if row is None: raise ValueError(f"no treaty with ID {treatyID} found")
    treatyArgs = row[0]
    
    if treatyArgs is None: raise ValueError(f"treaty_args column for treaty with ID {treatyID} is null")
    treatyArgs = json.loads(treatyArgs)
    if treatyArgs.get("clauses") is None: treatyArgs["clauses"] = []
    clauses = treatyArgs["clauses"]
    if len(clauses) <= clauseIndex: raise ValueError("clause no given does not exist")
    del clauses[clauseIndex]
    treatyArgs["clauses"] = clauses
    treatyArgs = json.dumps(treatyArgs)

    cursor.execute("""
        UPDATE treaties
        SET treaty_args = (?)
        WHERE treaty_id == (?)
    """, (treatyArgs, treatyID))

def DeleteConditionBlockFromDB(cursor: sqlite3.Cursor, treatyID: int, condType: str, blockIndex: int) -> None:
    cursor.execute("""
        SELECT treaty_args
        FROM treaties
        WHERE treaty_id == (?)
    """, (treatyID,))
    row = cursor.fetchone()
    if row is None: raise ValueError(f"no treaty with ID {treatyID} found")
    treatyArgs = row[0]
    
    if treatyArgs is None: raise ValueError(f"treaty_args column for treaty with ID {treatyID} is null")
    treatyArgs = json.loads(treatyArgs)
    if treatyArgs.get(condType) is None: treatyArgs[condType] = []
    blocks = treatyArgs[condType]
    if len(blocks) <= blockIndex: raise ValueError("blockIndex given does not exist")
    del blocks[blockIndex]
    
    treatyArgs = json.dumps(treatyArgs)

    cursor.execute("""
        UPDATE treaties
        SET treaty_args = (?)
        WHERE treaty_id == (?)
    """, (treatyArgs, treatyID))

def DeleteAutoConditionFromDB(cursor: sqlite3.Cursor, treatyID: int, condType: str, blockIndex: int, condIndex: int) -> None:
    cursor.execute("""
        SELECT treaty_args
        FROM treaties
        WHERE treaty_id == (?)
    """, (treatyID,))
    row = cursor.fetchone()
    if row is None: raise ValueError(f"no treaty with ID {treatyID} found")
    treatyArgs = row[0]
    
    if treatyArgs is None: raise ValueError(f"treaty_args column for treaty with ID {treatyID} is null")
    treatyArgs = json.loads(treatyArgs)
    if condType == "clause":
        if treatyArgs.get("clauses") is None: treatyArgs["clauses"] = []
        clauses = treatyArgs["clauses"]
        if len(clauses) <= blockIndex: raise ValueError(f"block index {blockIndex} given does not exist")
        clause = clauses[blockIndex]
        if clause.get("conditions") is None: clause["conditions"] = []
        conditions = clause["conditions"]
        if len(conditions) <= condIndex: raise ValueError(f"cond index {condIndex} given does not exist")
        del conditions[condIndex]
    else:
        if treatyArgs.get(condType) is None: treatyArgs[condType] = []
        condBlocks = treatyArgs[condType]
        if len(condBlocks) <= blockIndex: raise ValueError(f"block index {blockIndex} given does not exist")
        condBlock = condBlocks[blockIndex]
        if condBlock["blockType"] != "auto": raise ValueError(f"cannot add condition to block of type{condBlock["blockType"]}")
        if condBlock.get("conditions") is None: condBlock["conditions"] = []
        conditions = condBlock["conditions"]
        if len(conditions) <= condIndex: raise ValueError(f"cond index {condIndex} given does not exist")
        del conditions[condIndex]

    treatyArgs = json.dumps(treatyArgs)

    cursor.execute("""
        UPDATE treaties
        SET treaty_args = (?)
        WHERE treaty_id == (?)
    """, (treatyArgs, treatyID))

def DeleteVoteConditionFromDB(cursor: sqlite3.Cursor, treatyID: int, condType: str, blockIndex: int, condIndex: int) -> None:
    cursor.execute("""
        SELECT treaty_args
        FROM treaties
        WHERE treaty_id == (?)
    """, (treatyID,))
    row = cursor.fetchone()
    if row is None: raise ValueError(f"no treaty with ID {treatyID} found")
    treatyArgs = row[0]
    
    if treatyArgs is None: raise ValueError(f"treaty_args column for treaty with ID {treatyID} is null")
    treatyArgs = json.loads(treatyArgs)
    if condType == "clause":
        raise ValueError("vote condition cannot have condType 'clause'")
    else:
        if treatyArgs.get(condType) is None: treatyArgs[condType] = []
        condBlocks = treatyArgs[condType]
        if len(condBlocks) <= blockIndex: raise ValueError(f"block index {blockIndex} given does not exist")
        condBlock = condBlocks[blockIndex]
        if condBlock["blockType"] != "vote": raise ValueError(f"cannot add condition to block of type{condBlock["blockType"]}")
        if condBlock.get("conditions") is None: condBlock["conditions"] = []
        conditions = condBlock["conditions"]
        if len(conditions) <= condIndex: raise ValueError(f"cond index {condIndex} given does not exist")
        del conditions[condIndex]

    treatyArgs = json.dumps(treatyArgs)

    cursor.execute("""
        UPDATE treaties
        SET treaty_args = (?)
        WHERE treaty_id == (?)
    """, (treatyArgs, treatyID))

def DeleteSignatoryFromDB(cursor: sqlite3.Cursor, treatyID: int, countryID: int) -> None:
    cursor.execute("""
        SELECT signed_treaties
        FROM playernations
        WHERE nation_id == (?)
    """, (countryID,))
    row = cursor.fetchone()
    if row is None: raise ValueError(f"no country with ID {countryID} found")
    treatyInfoList = row[0]
    
    if treatyInfoList is None: raise ValueError(f"signed_treaties column for country with ID {countryID} is null")
    treatyInfoList = json.loads(treatyInfoList)
    
    treatyInfoIndex: int | None = None
    for i, treatyInfo in enumerate(treatyInfoList):
        if treatyInfo["treatyID"] == treatyID:
            treatyInfoIndex = i
    
    if treatyInfoIndex is not None:
        del treatyInfoList[treatyInfoIndex]

    treatyInfoList = json.dumps(treatyInfoList)

    cursor.execute("""
        UPDATE playernations
        SET signed_treaties = (?)
        WHERE nation_id == (?)
    """, (treatyInfoList, countryID))

def GetClauseEnumFromIndex(cursor: sqlite3.Cursor, treatyID: int, clauseIndex: int) -> TreatyClauses:
    cursor.execute("""
        SELECT treaty_args
        FROM treaties
        WHERE treaty_id == (?)
    """, (treatyID,))
    row = cursor.fetchone()
    if row is None: raise ValueError(f"no treaty with ID {treatyID} found")
    treatyArgs = row[0]
    
    if treatyArgs is None: raise ValueError(f"treaty_args column for treaty with ID {treatyID} is null")
    treatyArgs = json.loads(treatyArgs)
    if treatyArgs.get("clauses") is None: treatyArgs["clauses"] = []
    clauses = treatyArgs["clauses"]
    if len(clauses) <= clauseIndex: raise ValueError("clause no given does not exist")
    clause = clauses[clauseIndex]
    clauseEnum = TreatyClauses(clause["clauseEnum"])

    return clauseEnum

def GetConditionBlockType(cursor: sqlite3.Cursor, treatyID: int, condType: str, blockIndex: int) -> str:
    cursor.execute("""
        SELECT treaty_args
        FROM treaties
        WHERE treaty_id == (?)
    """, (treatyID,))
    row = cursor.fetchone()
    if row is None: raise ValueError(f"no treaty with ID {treatyID} found")
    treatyArgs = row[0]
    
    if treatyArgs is None: raise ValueError(f"treaty_args column for treaty with ID {treatyID} is null")
    treatyArgs = json.loads(treatyArgs)
    if treatyArgs.get(condType) is None: treatyArgs[condType] = []
    condBlocks = treatyArgs[condType]
    if len(condBlocks) <= blockIndex: raise ValueError("blockIndex given does not exist")
    blockType = condBlocks[blockIndex]["blockType"]
    if blockType not in ["auto","vote"]: raise ValueError("blockType must be either 'auto' or 'vote'")
    
    return blockType