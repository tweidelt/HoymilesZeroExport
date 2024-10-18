#!/usr/bin/env python3

# HoymilesZeroExport - https://github.com/reserve85/HoymilesZeroExport
# Copyright (C) 2023, Tobias Kraft

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

__author__ = "Tobias Kraft"
__version__ = "1.104"

import time
from requests.sessions import Session
from requests.auth import HTTPBasicAuth
from requests.auth import HTTPDigestAuth
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
import os
import logging
from logging.handlers import TimedRotatingFileHandler
from configparser import ConfigParser
from pathlib import Path
import sys
from packaging import version
import argparse
import subprocess
from config_provider import ConfigFileConfigProvider, MqttHandler, ConfigProviderChain
import json
from pyModbusTCP.client import ModbusClient
import struct

session = Session()
logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger()

parser = argparse.ArgumentParser()
parser.add_argument('-c', '--config', help='Override configuration file path')
args = parser.parse_args()

try:
    config = ConfigParser()

    baseconfig = str(Path.joinpath(Path(__file__).parent.resolve(), "HoymilesZeroExport_Config.ini"))
    if args.config:
        config.read([baseconfig, args.config])
    else:
        config.read(baseconfig)

    ENABLE_LOG_TO_FILE = config.getboolean('COMMON', 'ENABLE_LOG_TO_FILE')
    LOG_BACKUP_COUNT = config.getint('COMMON', 'LOG_BACKUP_COUNT')
except Exception as e:
    logger.info("Error on reading ENABLE_LOG_TO_FILE, set it to DISABLED")
    ENABLE_LOG_TO_FILE = False
    if hasattr(e, "message"):
        logger.error(e.message)
    else:
        logger.error(e)

if ENABLE_LOG_TO_FILE:
    if not os.path.exists(Path.joinpath(Path(__file__).parent.resolve(), "log")):
        os.makedirs(Path.joinpath(Path(__file__).parent.resolve(), "log"))

    rotating_file_handler = TimedRotatingFileHandler(
        filename=Path.joinpath(
            Path.joinpath(Path(__file__).parent.resolve(), "log"), "log"
        ),
        when="midnight",
        interval=2,
        backupCount=LOG_BACKUP_COUNT,
    )

    formatter = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s")
    rotating_file_handler.setFormatter(formatter)
    logger.addHandler(rotating_file_handler)

logger.info("Log write to file: %s", ENABLE_LOG_TO_FILE)
logger.info("Python Version: " + sys.version)
try:
    assert sys.version_info >= (3,8)
except:
    logger.info('Error: your Python version is too old, this script requires version 3.8 or newer. Please update your Python.')
    sys.exit()


def CastToInt(pValueToCast):
    try:
        result = int(pValueToCast)
        return result
    except:
        result = 0
    try:
        result = int(float(pValueToCast))
        return result
    except:
        logger.error("Exception at CastToInt")
        raise

def SetLimit(pLimit):
    try:
        if not hasattr(SetLimit, "LastLimit"):
            SetLimit.LastLimit = CastToInt(0)
        if not hasattr(SetLimit, "LastLimitAck"):
            SetLimit.LastLimitAck = bool(False)
        if (SetLimit.LastLimit == CastToInt(pLimit)) and SetLimit.LastLimitAck:
            logger.info("Inverterlimit was already accepted at %s Watt",CastToInt(pLimit))
            CrossCheckLimit()
            return
        if (SetLimit.LastLimit == CastToInt(pLimit)) and not SetLimit.LastLimitAck:
            logger.info("Inverterlimit %s Watt was previously not accepted by at least one inverter, trying again...",CastToInt(pLimit))

        logger.info("setting new limit to %s Watt",CastToInt(pLimit))
        SetLimit.LastLimit = CastToInt(pLimit)
        SetLimit.LastLimitAck = True

        min_watt_all_inverters = GetMinWattFromAllInverters()
        if (CastToInt(pLimit) <= min_watt_all_inverters):
            pLimit = min_watt_all_inverters # set only minWatt for every inv.
            PublishGlobalState("limit", min_watt_all_inverters)
        else:
            PublishGlobalState("limit", CastToInt(pLimit))

        RemainingLimit = CastToInt(pLimit)

        RemainingLimit -= GetMinWattFromAllInverters()

        # Handle non-battery inverters first
        if RemainingLimit >= GetMaxWattFromAllNonBatteryInverters() - GetMinWattFromAllNonBatteryInverters():
            nonBatteryInvertersLimit = GetMaxWattFromAllNonBatteryInverters() - GetMinWattFromAllNonBatteryInverters()
        else:
            nonBatteryInvertersLimit = RemainingLimit

        for i in range(INVERTER_COUNT):
            if not AVAILABLE[i] or HOY_BATTERY_MODE[i]:
                continue

            # Calculate proportional limit for non-battery inverters
            NewLimit = CastToInt(nonBatteryInvertersLimit * (HOY_MAX_WATT[i] - GetMinWatt(i)) / (GetMaxWattFromAllNonBatteryInverters() - GetMinWattFromAllNonBatteryInverters()))

            NewLimit += GetMinWatt(i)

            # Apply the calculated limit to the inverter
            NewLimit = ApplyLimitsToSetpointInverter(i, NewLimit)
            if HOY_COMPENSATE_WATT_FACTOR[i] != 1:
                logger.info(
                    'Ahoy: Inverter "%s": compensate Limit from %s Watt to %s Watt',
                    NAME[i],
                    CastToInt(NewLimit),
                    CastToInt(NewLimit * HOY_COMPENSATE_WATT_FACTOR[i]),
                )
                NewLimit = CastToInt(NewLimit * HOY_COMPENSATE_WATT_FACTOR[i])
                NewLimit = ApplyLimitsToMaxInverterLimits(i, NewLimit)

            if (NewLimit == CastToInt(CURRENT_LIMIT[i])) and LASTLIMITACKNOWLEDGED[i]:
                logger.info('Inverter "%s": Already at %s Watt',NAME[i],CastToInt(NewLimit))
                continue

            LASTLIMITACKNOWLEDGED[i] = True

            PublishInverterState(i, "limit", NewLimit)
            DTU.SetLimit(i, NewLimit)
            if not DTU.WaitForAck(i, SET_LIMIT_TIMEOUT_SECONDS):
                SetLimit.LastLimitAck = False
                LASTLIMITACKNOWLEDGED[i] = False

        # Adjust RemainingLimit based on what was assigned to non-battery inverters
        RemainingLimit -= nonBatteryInvertersLimit

        # Then handle battery inverters based on priority
        for j in range(1, 6):
            batteryMaxWattSamePrio = GetMaxWattFromAllBatteryInvertersSamePrio(j)
            if batteryMaxWattSamePrio <= 0:
                continue

            if RemainingLimit >= batteryMaxWattSamePrio - GetMinWattFromAllBatteryInvertersWithSamePriority(j):
                LimitPrio = batteryMaxWattSamePrio - GetMinWattFromAllBatteryInvertersWithSamePriority(j)
            else:
                LimitPrio = RemainingLimit

            for i in range(INVERTER_COUNT):
                if (not HOY_BATTERY_MODE[i]):
                    continue
                if (not AVAILABLE[i]) or (not HOY_BATTERY_GOOD_VOLTAGE[i]):
                    continue
                if CONFIG_PROVIDER.get_battery_priority(i) != j:
                    continue

                # Calculate proportional limit for battery inverters
                NewLimit = CastToInt(LimitPrio * (HOY_MAX_WATT[i] - GetMinWatt(i)) / (GetMaxWattFromAllBatteryInvertersSamePrio(j) - GetMinWattFromAllBatteryInvertersWithSamePriority(j)))
                NewLimit += GetMinWatt(i)

                NewLimit = ApplyLimitsToSetpointInverter(i, NewLimit)
                if HOY_COMPENSATE_WATT_FACTOR[i] != 1:
                    logger.info('Ahoy: Inverter "%s": compensate Limit from %s Watt to %s Watt', NAME[i], CastToInt(NewLimit), CastToInt(NewLimit*HOY_COMPENSATE_WATT_FACTOR[i]))
                    NewLimit = CastToInt(NewLimit * HOY_COMPENSATE_WATT_FACTOR[i])
                    NewLimit = ApplyLimitsToMaxInverterLimits(i, NewLimit)

                if (NewLimit == CastToInt(CURRENT_LIMIT[i])) and LASTLIMITACKNOWLEDGED[i]:
                    logger.info('Inverter "%s": Already at %s Watt',NAME[i],CastToInt(NewLimit))
                    continue

                LASTLIMITACKNOWLEDGED[i] = True

                PublishInverterState(i, "limit", NewLimit)
                DTU.SetLimit(i, NewLimit)
                if not DTU.WaitForAck(i, SET_LIMIT_TIMEOUT_SECONDS):
                    SetLimit.LastLimitAck = False
                    LASTLIMITACKNOWLEDGED[i] = False

            RemainingLimit -= LimitPrio
    except:
        logger.error("Exception at SetLimit")
        SetLimit.LastLimitAck = False
        raise

def ResetInverterData(pInverterId):
    attributes_to_delete = [
        "LastLimit",
        "LastLimitAck",
    ]
    array_attributes_to_delete = [
        {"LastPowerStatus": False},
        {"SamePowerStatusCnt": 0},
    ]
    target_objects = [
        SetLimit,
        GetHoymilesPanelMinVoltage,
    ]
    for target_object in target_objects:
        for attribute in attributes_to_delete:
            if hasattr(target_object, attribute):
                delattr(target_object, attribute)
        for array_attribute in array_attributes_to_delete:
            for key, value in array_attribute.items():
                if hasattr(target_object, key):
                    target_object[key][pInverterId] = value

    LASTLIMITACKNOWLEDGED[pInverterId] = False
    HOY_PANEL_MIN_VOLTAGE_HISTORY_LIST[pInverterId] = []
    CURRENT_LIMIT[pInverterId] = -1
    HOY_BATTERY_GOOD_VOLTAGE[pInverterId] = True
    TEMPERATURE[pInverterId] = str('--- degC')


def GetHoymilesAvailable():
    try:
        GetHoymilesAvailable = False
        for i in range(INVERTER_COUNT):
            try:
                WasAvail = AVAILABLE[i]
                AVAILABLE[i] = ENABLED[i] and DTU.GetAvailable(i)
                if AVAILABLE[i]:
                    GetHoymilesAvailable = True
                    if not WasAvail:
                        ResetInverterData(i)
                        GetHoymilesInfo()
            except Exception as e:
                AVAILABLE[i] = False
                logger.error(
                    "Exception at GetHoymilesAvailable, Inverter %s (%s) not reachable",
                    i,
                    NAME[i],
                )
                if hasattr(e, "message"):
                    logger.error(e.message)
                else:
                    logger.error(e)
        return GetHoymilesAvailable
    except:
        logger.error("Exception at GetHoymilesAvailable")
        raise

def GetHoymilesInfo():
    try:
        for i in range(INVERTER_COUNT):
            try:
                if not AVAILABLE[i]:
                    continue
                DTU.GetInfo(i)
            except Exception as e:
                logger.error('Exception at GetHoymilesInfo, Inverter "%s" not reachable', NAME[i])
                if hasattr(e, 'message'):
                    logger.error(e.message)
                else:
                    logger.error(e)
    except:
        logger.error("Exception at GetHoymilesInfo")
        raise

def GetHoymilesPanelMinVoltage(pInverterId):
    try:
        if not AVAILABLE[pInverterId]:
            return 0

        HOY_PANEL_MIN_VOLTAGE_HISTORY_LIST[pInverterId].append(DTU.GetPanelMinVoltage(pInverterId))

        # calculate mean over last x values
        if len(HOY_PANEL_MIN_VOLTAGE_HISTORY_LIST[pInverterId]) > HOY_BATTERY_AVERAGE_CNT[pInverterId]:
            HOY_PANEL_MIN_VOLTAGE_HISTORY_LIST[pInverterId].pop(0)
        from statistics import mean

        logger.info('Average min-panel voltage, inverter "%s": %s Volt',NAME[pInverterId], mean(HOY_PANEL_MIN_VOLTAGE_HISTORY_LIST[pInverterId]))
        return mean(HOY_PANEL_MIN_VOLTAGE_HISTORY_LIST[pInverterId])
    except:
        logger.error("Exception at GetHoymilesPanelMinVoltage, Inverter %s not reachable", pInverterId)
        raise

def SetHoymilesPowerStatus(pInverterId, pActive):
    try:
        if not AVAILABLE[pInverterId]:
            return
        if SET_POWERSTATUS_CNT > 0:
            if not hasattr(SetHoymilesPowerStatus, "LastPowerStatus"):
                SetHoymilesPowerStatus.LastPowerStatus = []
                SetHoymilesPowerStatus.LastPowerStatus = [False for i in range(INVERTER_COUNT)]
            if not hasattr(SetHoymilesPowerStatus, "SamePowerStatusCnt"):
                SetHoymilesPowerStatus.SamePowerStatusCnt = []
                SetHoymilesPowerStatus.SamePowerStatusCnt = [0 for i in range(INVERTER_COUNT)]
            if SetHoymilesPowerStatus.LastPowerStatus[pInverterId] == pActive:
                SetHoymilesPowerStatus.SamePowerStatusCnt[pInverterId] = SetHoymilesPowerStatus.SamePowerStatusCnt[pInverterId] + 1
            else:
                SetHoymilesPowerStatus.LastPowerStatus[pInverterId] = pActive
                SetHoymilesPowerStatus.SamePowerStatusCnt[pInverterId] = 0
            if SetHoymilesPowerStatus.SamePowerStatusCnt[pInverterId] > SET_POWERSTATUS_CNT:
                if pActive:
                    logger.info(
                        "Retry Counter exceeded: Inverter PowerStatus already ON"
                    )
                else:
                    logger.info(
                        "Retry Counter exceeded: Inverter PowerStatus already OFF"
                    )
                return
        DTU.SetPowerStatus(pInverterId, pActive)
        time.sleep(SET_POWER_STATUS_DELAY_IN_SECONDS)
    except:
        logger.error("Exception at SetHoymilesPowerStatus")
        raise


def GetNumberArray(pExcludedPanels):
    lclExcludedPanelsList = pExcludedPanels.split(",")
    result = []
    for number_str in lclExcludedPanelsList:
        if number_str == "":
            continue
        number = int(number_str.strip())
        result.append(number)
    return result


def GetCheckBattery():
    try:
        result = False
        for i in range(INVERTER_COUNT):
            try:
                if not AVAILABLE[i]:
                    continue
                if not HOY_BATTERY_MODE[i]:
                    result = True
                    continue
                minVoltage = GetHoymilesPanelMinVoltage(i)

                if minVoltage <= HOY_BATTERY_THRESHOLD_OFF_LIMIT_IN_V[i]:
                    SetHoymilesPowerStatus(i, False)
                    HOY_BATTERY_GOOD_VOLTAGE[i] = False
                    HOY_MAX_WATT[i] = CONFIG_PROVIDER.get_reduce_wattage(i)

                elif minVoltage <= HOY_BATTERY_THRESHOLD_REDUCE_LIMIT_IN_V[i]:
                    if HOY_MAX_WATT[i] != CONFIG_PROVIDER.get_reduce_wattage(i):
                        HOY_MAX_WATT[i] = CONFIG_PROVIDER.get_reduce_wattage(i)
                        SetLimit.LastLimit = -1

                elif minVoltage >= HOY_BATTERY_THRESHOLD_ON_LIMIT_IN_V[i]:
                    SetHoymilesPowerStatus(i, True)
                    if not HOY_BATTERY_GOOD_VOLTAGE[i]:
                        DTU.SetLimit(i, GetMinWatt(i))
                        DTU.WaitForAck(i, SET_LIMIT_TIMEOUT_SECONDS)
                        SetLimit.LastLimit = -1
                    HOY_BATTERY_GOOD_VOLTAGE[i] = True
                    if (minVoltage >= HOY_BATTERY_THRESHOLD_NORMAL_LIMIT_IN_V[i]) and (HOY_MAX_WATT[i] != CONFIG_PROVIDER.get_normal_wattage(i)):
                        HOY_MAX_WATT[i] = CONFIG_PROVIDER.get_normal_wattage(i)
                        SetLimit.LastLimit = -1

                elif minVoltage >= HOY_BATTERY_THRESHOLD_NORMAL_LIMIT_IN_V[i]:
                    if HOY_MAX_WATT[i] != CONFIG_PROVIDER.get_normal_wattage(i):
                        HOY_MAX_WATT[i] = CONFIG_PROVIDER.get_normal_wattage(i)
                        SetLimit.LastLimit = -1

                if HOY_BATTERY_GOOD_VOLTAGE[i]:
                    result = True
            except:
                logger.error("Exception at CheckBattery, Inverter %s not reachable", i)
        return result
    except:
        logger.error("Exception at CheckBattery")
        raise

def GetHoymilesTemperature():
    try:
        for i in range(INVERTER_COUNT):
            try:
                DTU.GetTemperature(i)
            except:
                logger.error(
                    "Exception at GetHoymilesTemperature, Inverter %s not reachable", i
                )
    except:
        logger.error("Exception at GetHoymilesTemperature")
        raise

def GetHoymilesActualPower():
    try:
        try:
            Watts = abs(INTERMEDIATE_POWERMETER.GetPowermeterWatts())
            logger.info(f"intermediate meter {INTERMEDIATE_POWERMETER.__class__.__name__}: {Watts} Watt")
            return Watts
        except Exception as e:
            logger.error("Exception at GetHoymilesActualPower")
            if hasattr(e, 'message'):
                logger.error(e.message)
            else:
                logger.error(e)
            logger.error("try reading actual power from DTU:")
            Watts = DTU.GetPowermeterWatts()
            logger.info(f"intermediate meter {DTU.__class__.__name__}: {Watts} Watt")
    except:
        logger.error("Exception at GetHoymilesActualPower")
        if SET_INVERTER_TO_MIN_ON_POWERMETER_ERROR:
            SetLimit(0)
        raise

def GetPowermeterWatts():
    try:
        Watts = POWERMETER.GetPowermeterWatts()
        logger.info(f"powermeter {POWERMETER.__class__.__name__}: {Watts} Watt")
        return Watts
    except:
        logger.error("Exception at GetPowermeterWatts")
        if SET_INVERTER_TO_MIN_ON_POWERMETER_ERROR:
            SetLimit(0)
        raise

def GetMinWatt(pInverter: int):
    min_watt_percent = CONFIG_PROVIDER.get_min_wattage_in_percent(pInverter)
    return int(HOY_INVERTER_WATT[pInverter] * min_watt_percent / 100)

def CutLimitToProduction(pSetpoint):
    if pSetpoint != GetMaxWattFromAllInverters():
        ActualPower = GetHoymilesActualPower()
        # prevent the setpoint from running away...
        if pSetpoint > ActualPower + (
            GetMaxWattFromAllInverters()
            * MAX_DIFFERENCE_BETWEEN_LIMIT_AND_OUTPUTPOWER
            / 100
        ):
            pSetpoint = CastToInt(
                ActualPower
                + (
                    GetMaxWattFromAllInverters()
                    * MAX_DIFFERENCE_BETWEEN_LIMIT_AND_OUTPUTPOWER
                    / 100
                )
            )
            logger.info(
                "Cut limit to %s Watt, limit was higher than %s percent of live-production",
                CastToInt(pSetpoint),
                MAX_DIFFERENCE_BETWEEN_LIMIT_AND_OUTPUTPOWER,
            )
    return CastToInt(pSetpoint)


def ApplyLimitsToSetpoint(pSetpoint):
    if pSetpoint > GetMaxWattFromAllInverters():
        pSetpoint = GetMaxWattFromAllInverters()
    if pSetpoint < GetMinWattFromAllInverters():
        pSetpoint = GetMinWattFromAllInverters()
    return pSetpoint


def ApplyLimitsToSetpointInverter(pInverter, pSetpoint):
    if pSetpoint > HOY_MAX_WATT[pInverter]:
        pSetpoint = HOY_MAX_WATT[pInverter]
    if pSetpoint < GetMinWatt(pInverter):
        pSetpoint = GetMinWatt(pInverter)
    return pSetpoint


def ApplyLimitsToMaxInverterLimits(pInverter, pSetpoint):
    if pSetpoint > HOY_INVERTER_WATT[pInverter]:
        pSetpoint = HOY_INVERTER_WATT[pInverter]
    if pSetpoint < GetMinWatt(pInverter):
        pSetpoint = GetMinWatt(pInverter)
    return pSetpoint

def CrossCheckLimit():
    try:
        for i in range(INVERTER_COUNT):
            if AVAILABLE[i]:
                DTULimitInW = DTU.GetActualLimitInW(i)
                LimitMax = float(CURRENT_LIMIT[i] + HOY_INVERTER_WATT[i] * 0.05)
                LimitMin = float(CURRENT_LIMIT[i] - HOY_INVERTER_WATT[i] * 0.05)
                if not (min(LimitMax, LimitMin) < DTULimitInW < max(LimitMax, LimitMin)):
                    logger.info('CrossCheckLimit: DTU ( %s ) <> SetLimit ( %s ). Resend limit to DTU', "{:.1f}".format(DTULimitInW), "{:.1f}".format(CURRENT_LIMIT[i]))
                    DTU.SetLimit(i, CURRENT_LIMIT[i])
    except:
        logger.error("Exception at CrossCheckLimit")
        raise

def GetMaxWattFromAllInverters():
    # Max possible Watts, can be reduced on battery mode
    maxWatt = 0
    for i in range(INVERTER_COUNT):
        if (not AVAILABLE[i]) or (not HOY_BATTERY_GOOD_VOLTAGE[i]):
            continue
        maxWatt = maxWatt + HOY_MAX_WATT[i]
    return maxWatt

def GetMaxWattFromAllBatteryInvertersSamePrio(pPriority):
    return sum(
        HOY_MAX_WATT[i] for i in range(INVERTER_COUNT)
        if AVAILABLE[i] and HOY_BATTERY_GOOD_VOLTAGE[i] and HOY_BATTERY_MODE[i] and CONFIG_PROVIDER.get_battery_priority(i) == pPriority
    )

def GetMaxInverterWattFromAllInverters():
    # Max possible Watts (physically) - Inverter Specification!
    maxWatt = 0
    for i in range(INVERTER_COUNT):
        if (not AVAILABLE[i]) or (not HOY_BATTERY_GOOD_VOLTAGE[i]):
            continue
        maxWatt = maxWatt + HOY_INVERTER_WATT[i]
    return maxWatt

def GetMaxWattFromAllNonBatteryInverters():
    return sum(
        HOY_MAX_WATT[i] for i in range(INVERTER_COUNT)
        if AVAILABLE[i] and not HOY_BATTERY_MODE[i] and HOY_BATTERY_GOOD_VOLTAGE[i]
    )

def GetMinWattFromAllInverters():
    minWatt = 0
    for i in range(INVERTER_COUNT):
        if (not AVAILABLE[i]) or (not HOY_BATTERY_GOOD_VOLTAGE[i]):
            continue
        minWatt = minWatt + GetMinWatt(i)
    return minWatt

def GetMinWattFromAllNonBatteryInverters():
    minWatt = 0
    for i in range(INVERTER_COUNT):
        if (not AVAILABLE[i]) or (HOY_BATTERY_MODE[i]) or (not HOY_BATTERY_GOOD_VOLTAGE[i]):
            continue
        minWatt = minWatt + GetMinWatt(i)
    return minWatt

def GetMinWattFromAllBatteryInverters():
    minWatt = 0
    for i in range(INVERTER_COUNT):
        if (not AVAILABLE[i]) or (not HOY_BATTERY_MODE[i]) or (not HOY_BATTERY_GOOD_VOLTAGE[i]):
            continue
        minWatt = minWatt + GetMinWatt(i)
    return minWatt

def GetMinWattFromAllBatteryInvertersWithSamePriority(pPriority):
    minWatt = 0
    for i in range(INVERTER_COUNT):
        if (not AVAILABLE[i]) or (not HOY_BATTERY_MODE[i]) or (not HOY_BATTERY_GOOD_VOLTAGE[i]) or (CONFIG_PROVIDER.get_battery_priority(i) != pPriority):
            continue
        minWatt = minWatt + GetMinWatt(i)
    return minWatt

def PublishConfigState():
    if MQTT is None:
        return
    MQTT.publish_state("on_grid_usage_jump_to_limit_percent", CONFIG_PROVIDER.on_grid_usage_jump_to_limit_percent())
    MQTT.publish_state("on_grid_feed_fast_limit_decrease", CONFIG_PROVIDER.on_grid_feed_fast_limit_decrease())
    MQTT.publish_state("powermeter_target_point", CONFIG_PROVIDER.get_powermeter_target_point())
    MQTT.publish_state("powermeter_max_point", CONFIG_PROVIDER.get_powermeter_max_point())
    MQTT.publish_state("powermeter_min_point", CONFIG_PROVIDER.get_powermeter_min_point())
    MQTT.publish_state("powermeter_tolerance", CONFIG_PROVIDER.get_powermeter_tolerance())
    MQTT.publish_state("inverter_count", INVERTER_COUNT)
    for i in range(INVERTER_COUNT):
        MQTT.publish_inverter_state(i, "min_watt_in_percent", CONFIG_PROVIDER.get_min_wattage_in_percent(i))
        MQTT.publish_inverter_state(i, "normal_watt", CONFIG_PROVIDER.get_normal_wattage(i))
        MQTT.publish_inverter_state(i, "reduce_watt", CONFIG_PROVIDER.get_reduce_wattage(i))
        MQTT.publish_inverter_state(i, "battery_priority", CONFIG_PROVIDER.get_battery_priority(i))

def PublishGlobalState(state_name, state_value):
    if MQTT is None:
        return
    MQTT.publish_state(state_name, state_value)

def PublishInverterState(inverter_idx, state_name, state_value):
    if MQTT is None:
        return
    MQTT.publish_inverter_state(inverter_idx, state_name, state_value)

class Powermeter:
    def GetPowermeterWatts(self) -> int:
        raise NotImplementedError()

class Tasmota(Powermeter):
    def __init__(self, ip: str, user: str, password: str, json_status: str, json_payload_mqtt_prefix: str, json_power_mqtt_label: str, json_power_input_mqtt_label: str, json_power_output_mqtt_label: str, json_power_calculate: bool):
        self.ip = ip
        self.user = user
        self.password = password
        self.json_status = json_status
        self.json_payload_mqtt_prefix = json_payload_mqtt_prefix
        self.json_power_mqtt_label = json_power_mqtt_label
        self.json_power_input_mqtt_label = json_power_input_mqtt_label
        self.json_power_output_mqtt_label = json_power_output_mqtt_label
        self.json_power_calculate = json_power_calculate

    def GetJson(self, path):
        url = f'http://{self.ip}{path}'
        r = session.get(url, timeout=10)
        r.raise_for_status()
        return r.json()

    def GetPowermeterWatts(self):
        if not self.user:
            ParsedData = self.GetJson('/cm?cmnd=status%2010')
        else:
            ParsedData = self.GetJson(f'/cm?user={self.user}&password={self.password}&cmnd=status%2010')
        if not self.json_power_calculate:
            return CastToInt(ParsedData[self.json_status][self.json_payload_mqtt_prefix][self.json_power_mqtt_label])
        else:
            input = ParsedData[self.json_status][self.json_payload_mqtt_prefix][self.json_power_input_mqtt_label]
            ouput = ParsedData[self.json_status][self.json_payload_mqtt_prefix][self.json_power_output_mqtt_label]
            return CastToInt(input - ouput)

class Shelly(Powermeter):
    def __init__(self, ip: str, user: str, password: str, emeterindex: str):
        self.ip = ip
        self.user = user
        self.password = password
        self.emeterindex = emeterindex

    def GetJson(self, path):
        url = f'http://{self.ip}{path}'
        headers = {"content-type": "application/json"}
        r = session.get(url, headers=headers, auth=(self.user, self.password), timeout=10)
        r.raise_for_status()
        return r.json()

    def GetRpcJson(self, path):
        url = f'http://{self.ip}/rpc{path}'
        headers = {"content-type": "application/json"}
        r = session.get(url, headers=headers, auth=HTTPDigestAuth(self.user, self.password), timeout=10)
        r.raise_for_status()
        return r.json()

    def GetPowermeterWatts(self) -> int:
        raise NotImplementedError()

class Shelly1PM(Shelly):
    def GetPowermeterWatts(self):
        return CastToInt(self.GetJson('/status')['meters'][0]['power'])

class ShellyPlus1PM(Shelly):
    def GetPowermeterWatts(self):
        return CastToInt(self.GetRpcJson('/Switch.GetStatus?id=0')['apower'])

class ShellyEM(Shelly):
    def GetPowermeterWatts(self):
        if self.emeterindex:
            return CastToInt(self.GetJson(f'/emeter/{self.emeterindex}')['power'])
        else:
            return sum(CastToInt(emeter['power']) for emeter in self.GetJson('/status')['emeters'])

class Shelly3EM(Shelly):
    def GetPowermeterWatts(self):
        return CastToInt(self.GetJson('/status')['total_power'])

class Shelly3EMPro(Shelly):
    def GetPowermeterWatts(self):
        return CastToInt(self.GetRpcJson('/EM.GetStatus?id=0')['total_act_power'])

class ESPHome(Powermeter):
    def __init__(self, ip: str, port: str, domain: str, id: str):
        self.ip = ip
        self.port = port
        self.domain = domain
        self.id = id

    def GetJson(self, path):
        url = f'http://{self.ip}:{self.port}{path}'
        r = session.get(url, timeout=10)
        r.raise_for_status()
        return r.json()

    def GetPowermeterWatts(self):
        ParsedData = self.GetJson(f'/{self.domain}/{self.id}')
        return CastToInt(ParsedData['value'])

class Shrdzm(Powermeter):
    def __init__(self, ip: str, user: str, password: str):
        self.ip = ip
        self.user = user
        self.password = password

    def GetJson(self, path):
        url = f'http://{self.ip}{path}'
        r = session.get(url, timeout=10)
        r.raise_for_status()
        return r.json()

    def GetPowermeterWatts(self):
        ParsedData = self.GetJson(f'/getLastData?user={self.user}&password={self.password}')
        return CastToInt(CastToInt(ParsedData['1.7.0']) - CastToInt(ParsedData['2.7.0']))

class Emlog(Powermeter):
    def __init__(self, ip: str, meterindex: str, json_power_calculate: bool):
        self.ip = ip
        self.meterindex = meterindex
        self.json_power_calculate = json_power_calculate

    def GetJson(self, path):
        url = f'http://{self.ip}{path}'
        r = session.get(url, timeout=10)
        r.raise_for_status()
        return r.json()

    def GetPowermeterWatts(self):
        ParsedData = self.GetJson(f'/pages/getinformation.php?heute&meterindex={self.meterindex}')
        if not self.json_power_calculate:
            return CastToInt(ParsedData['Leistung170'])
        else:
            input = ParsedData['Leistung170']
            ouput = ParsedData['Leistung270']
            return CastToInt(input - ouput)

class IoBroker(Powermeter):
    def __init__(self, ip: str, port: str, current_power_alias: str, power_calculate: bool, power_input_alias: str, power_output_alias: str):
        self.ip = ip
        self.port = port
        self.current_power_alias = current_power_alias
        self.power_calculate = power_calculate
        self.power_input_alias = power_input_alias
        self.power_output_alias = power_output_alias

    def GetJson(self, path):
        url = f'http://{self.ip}:{self.port}{path}'
        r = session.get(url, timeout=10)
        r.raise_for_status()
        return r.json()

    def GetPowermeterWatts(self):
        if not self.power_calculate:
            ParsedData = self.GetJson(f'/getBulk/{self.current_power_alias}')
            for item in ParsedData:
                if item['id'] == self.current_power_alias:
                    return CastToInt(item['val'])
        else:
            ParsedData = self.GetJson(f'/getBulk/{self.power_input_alias},{self.power_output_alias}')
            for item in ParsedData:
                if item['id'] == self.power_input_alias:
                    input = CastToInt(item['val'])
                if item['id'] == self.power_output_alias:
                    output = CastToInt(item['val'])
            return CastToInt(input - output)

class HomeAssistant(Powermeter):
    def __init__(self, ip: str, port: str, use_https: bool, access_token: str, current_power_entity: str, power_calculate: bool, power_input_alias: str, power_output_alias: str):
        self.ip = ip
        self.port = port
        self.use_https = use_https
        self.access_token = access_token
        self.current_power_entity = current_power_entity
        self.power_calculate = power_calculate
        self.power_input_alias = power_input_alias
        self.power_output_alias = power_output_alias

    def GetJson(self, path):
        if self.use_https:
            url = f"https://{self.ip}:{self.port}{path}"
        else:
            url = f"http://{self.ip}:{self.port}{path}"
        headers = {"Authorization": "Bearer " + self.access_token, "content-type": "application/json"}
        r = session.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        return r.json()

    def GetPowermeterWatts(self):
        if not self.power_calculate:
            ParsedData = self.GetJson(f"/api/states/{self.current_power_entity}")
            return CastToInt(ParsedData['state'])
        else:
            ParsedData = self.GetJson(f"/api/states/{self.power_input_alias}")
            input = CastToInt(ParsedData['state'])
            ParsedData = self.GetJson(f"/api/states/{self.power_output_alias}")
            output = CastToInt(ParsedData['state'])
            return CastToInt(input - output)

class VZLogger(Powermeter):
    def __init__(self, ip: str, port: str, uuid: str):
        self.ip = ip
        self.port = port
        self.uuid = uuid

    def GetJson(self):
        url = f"http://{self.ip}:{self.port}/{self.uuid}"
        r = session.get(url, timeout=10)
        r.raise_for_status()
        return r.json()

    def GetPowermeterWatts(self):
        return CastToInt(self.GetJson()['data'][0]['tuples'][0][1])

class AmisReader(Powermeter):
    def __init__(self, ip: str):
        self.ip = ip

    def GetJson(self, path):
        url = f'http://{self.ip}{path}'
        r = session.get(url, timeout=10)
        r.raise_for_status()
        return r.json()

    def GetPowermeterWatts(self):
        ParsedData = self.GetJson('/rest')
        return CastToInt(ParsedData['saldo'])

class ModbusTCP(Powermeter):
    def __init__(self, ip: str, unit_id: int, register: int, register_type: str, register_scale: float):
        self.register = register;
        self.register_type = register_type;
        self.register_scale = register_scale;
        self.modbusClient = ModbusClient(ip, 502, unit_id, auto_open=True)

    def GetPowermeterWatts(self):
        regCount = 2 if self.register_type == "int32" else 1
        regs = self.modbusClient.read_holding_registers(self.register, regCount)
        Watts = 0
        if regs is not None:
            if (self.register_type == "int32"):
                bytes = struct.pack('>HH', regs[1], regs[0])
                Watts = int.from_bytes(bytes, byteorder='big', signed=True) * self.register_scale
            else:
                Watts = CastToInt(regs[0]) * self.register_scale
        logger.info("powermeter ModbusTCP: %s %s", Watts, " Watt")
        return CastToInt(Watts)

class DebugReader(Powermeter):
    def GetPowermeterWatts(self):
        return CastToInt(input("Enter Powermeter Watts: "))

class DTU(Powermeter):
    def __init__(self, inverter_count: int):
        self.inverter_count = inverter_count

    def GetACPower(self, pInverterId: int):
        raise NotImplementedError()

    def GetPowermeterWatts(self):
        return sum(self.GetACPower(pInverterId) for pInverterId in range(self.inverter_count) if AVAILABLE[pInverterId] and HOY_BATTERY_GOOD_VOLTAGE[pInverterId])

    def CheckMinVersion(self):
        raise NotImplementedError()

    def GetAvailable(self, pInverterId: int):
        raise NotImplementedError()

    def GetActualLimitInW(self, pInverterId: int):
        raise NotImplementedError()

    def GetInfo(self, pInverterId: int):
        raise NotImplementedError()

    def GetTemperature(self, pInverterId: int):
        raise NotImplementedError()

    def GetPanelMinVoltage(self, pInverterId: int):
        raise NotImplementedError()

    def WaitForAck(self, pInverterId: int, pTimeoutInS: int):
        raise NotImplementedError()

    def SetLimit(self, pInverterId: int, pLimit: int):
        raise NotImplementedError()

    def SetPowerStatus(self, pInverterId: int, pActive: bool):
        raise NotImplementedError()

class AhoyDTU(DTU):
    def __init__(self, inverter_count: int, ip: str, password: str):
        super().__init__(inverter_count)
        self.ip = ip
        self.password = password
        self.Token = ''

    def GetJson(self, path):
        url = f'http://{self.ip}{path}'
        # AhoyDTU sometimes returns literal 'null' instead of a valid json, so we retry a few times
        data = None
        retry_count = 3
        while retry_count > 0 and data is None:
            data = session.get(url, timeout=10).json()
            retry_count -= 1
        return data

    def GetResponseJson(self, path, obj):
        url = f'http://{self.ip}{path}'
        r = session.post(url, json = obj, timeout=10)
        r.raise_for_status()
        return r.json()

    def GetACPower(self, pInverterId):
        ParsedData = self.GetJson('/api/live')
        ActualPower_index = ParsedData["ch0_fld_names"].index("P_AC")
        ParsedData = self.GetJson(f'/api/inverter/id/{pInverterId}')
        return CastToInt(ParsedData["ch"][0][ActualPower_index])

    def CheckMinVersion(self):
        MinVersion = '0.8.80'
        ParsedData = self.GetJson('/api/system')
        try:
            AhoyVersion = str((ParsedData["version"]))
        except:
            AhoyVersion = str((ParsedData["generic"]["version"]))
        logger.info('Ahoy: Current Version: %s',AhoyVersion)
        if version.parse(AhoyVersion) < version.parse(MinVersion):
            logger.error('Error: Your AHOY Version is too old! Please update at least to Version %s - you can find the newest dev-releases here: https://github.com/lumapu/ahoy/actions',MinVersion)
            quit()

    def GetAvailable(self, pInverterId: int):
        ParsedData = self.GetJson('/api/index')
        Available = bool(ParsedData["inverter"][pInverterId]["is_avail"])
        logger.info('Ahoy: Inverter "%s" Available: %s',NAME[pInverterId], Available)
        return Available

    def GetActualLimitInW(self, pInverterId: int):
        ParsedData = self.GetJson(f'/api/inverter/id/{pInverterId}')
        LimitInPercent = float(ParsedData['power_limit_read'])
        LimitInW = HOY_INVERTER_WATT[pInverterId] * LimitInPercent / 100
        return LimitInW

    def GetInfo(self, pInverterId: int):
        ParsedData = self.GetJson('/api/live')
        temp_index = ParsedData["ch0_fld_names"].index("Temp")

        ParsedData = self.GetJson(f'/api/inverter/id/{pInverterId}')
        SERIAL_NUMBER[pInverterId] = str(ParsedData['serial'])
        NAME[pInverterId] = str(ParsedData['name'])
        TEMPERATURE[pInverterId] = str(ParsedData["ch"][0][temp_index]) + ' degC'
        logger.info('Ahoy: Inverter "%s" / serial number "%s" / temperature %s',NAME[pInverterId],SERIAL_NUMBER[pInverterId],TEMPERATURE[pInverterId])

    def GetTemperature(self, pInverterId: int):
        ParsedData = self.GetJson('/api/live')
        temp_index = ParsedData["ch0_fld_names"].index("Temp")

        ParsedData = self.GetJson(f'/api/inverter/id/{pInverterId}')
        TEMPERATURE[pInverterId] = str(ParsedData["ch"][0][temp_index]) + ' degC'
        logger.info('Ahoy: Inverter "%s" temperature: %s',NAME[pInverterId],TEMPERATURE[pInverterId])

    def GetPanelMinVoltage(self, pInverterId: int):
        ParsedData = self.GetJson('/api/live')
        PanelVDC_index = ParsedData["fld_names"].index("U_DC")

        ParsedData = self.GetJson(f'/api/inverter/id/{pInverterId}')
        PanelVDC = []
        ExcludedPanels = GetNumberArray(HOY_BATTERY_IGNORE_PANELS[pInverterId])
        for i in range(1, len(ParsedData['ch']), 1):
            if i not in ExcludedPanels:
                PanelVDC.append(float(ParsedData['ch'][i][PanelVDC_index]))
        minVdc = float('inf')
        for i in range(len(PanelVDC)):
            if (minVdc > PanelVDC[i]) and (PanelVDC[i] > 5):
                minVdc = PanelVDC[i]
        if minVdc == float('inf'):
            minVdc = 0

        # save last 5 min-values in list and return the "highest" value.
        HOY_PANEL_VOLTAGE_LIST[pInverterId].append(minVdc)
        if len(HOY_PANEL_VOLTAGE_LIST[pInverterId]) > 5:
            HOY_PANEL_VOLTAGE_LIST[pInverterId].pop(0)
        max_value = None
        for num in HOY_PANEL_VOLTAGE_LIST[pInverterId]:
            if (max_value is None or num > max_value):
                max_value = num

        logger.info('Lowest panel voltage inverter "%s": %s Volt',NAME[pInverterId],max_value)
        return max_value

    def WaitForAck(self, pInverterId: int, pTimeoutInS: int):
        try:
            timeout = pTimeoutInS
            timeout_start = time.time()
            while time.time() < timeout_start + timeout:
                time.sleep(0.5)
                ParsedData = self.GetJson(f'/api/inverter/id/{pInverterId}')
                ack = bool(ParsedData['power_limit_ack'])
                if ack:
                    break
            if ack:
                logger.info('Ahoy: Inverter "%s": Limit acknowledged', NAME[pInverterId])
            else:
                logger.info('Ahoy: Inverter "%s": Limit timeout!', NAME[pInverterId])
            return ack
        except Exception as e:
            if hasattr(e, 'message'):
                logger.error('Ahoy: Inverter "%s" WaitForAck: "%s"', NAME[pInverterId], e.message)
            else:
                logger.error('Ahoy: Inverter "%s" WaitForAck: "%s"', NAME[pInverterId], e)
            return False

    def SetLimit(self, pInverterId: int, pLimit: int):
        logger.info('Ahoy: Inverter "%s": setting new limit from %s Watt to %s Watt',NAME[pInverterId],CastToInt(CURRENT_LIMIT[pInverterId]),CastToInt(pLimit))
        myobj = {'cmd': 'limit_nonpersistent_absolute', 'val': pLimit, "id": pInverterId, "token": self.Token}
        response = self.GetResponseJson('/api/ctrl', myobj)
        if response["success"] == False and response["error"] == "ERR_PROTECTED":
            self.Authenticate()
            self.SetLimit(pInverterId, pLimit)
            return
        if response["success"] == False:
            raise Exception("Error: SetLimitAhoy Request error")
        CURRENT_LIMIT[pInverterId] = pLimit

    def SetPowerStatus(self, pInverterId: int, pActive: bool):
        if pActive:
            logger.info('Ahoy: Inverter "%s": Turn on',NAME[pInverterId])
        else:
            logger.info('Ahoy: Inverter "%s": Turn off',NAME[pInverterId])
        myobj = {'cmd': 'power', 'val': CastToInt(pActive == True), "id": pInverterId, "token": self.Token}
        response = self.GetResponseJson('/api/ctrl', myobj)
        if response["success"] == False and response["error"] == "ERR_PROTECTED":
            self.Authenticate()
            self.SetPowerStatus(pInverterId, pActive)
            return
        if response["success"] == False:
            raise Exception("Error: SetPowerStatus Request error")

    def Authenticate(self):
        logger.info('Ahoy: Authenticating...')
        myobj = {'auth': self.password}
        response = self.GetResponseJson('/api/ctrl', myobj)
        if response["success"] == False:
            raise Exception("Error: Authenticate Request error")
        self.Token = response["token"]
        logger.info('Ahoy: Authenticating successful, received Token: %s', self.Token)

class OpenDTU(DTU):
    def __init__(self, inverter_count: int, ip: str, user: str, password: str):
        super().__init__(inverter_count)
        self.ip = ip
        self.user = user
        self.password = password

    def GetJson(self, path):
        url = f'http://{self.ip}{path}'
        r = session.get(url, auth=HTTPBasicAuth(self.user, self.password), timeout=10)
        r.raise_for_status()
        return r.json()

    def GetResponseJson(self, path, sendStr):
        url = f'http://{self.ip}{path}'
        headers = {'Content-Type': 'application/x-www-form-urlencoded'}
        r = session.post(url=url, headers=headers, data=sendStr, auth=HTTPBasicAuth(self.user, self.password), timeout=10)
        r.raise_for_status()
        return r.json()

    def GetACPower(self, pInverterId):
        ParsedData = self.GetJson(f'/api/livedata/status?inv={SERIAL_NUMBER[pInverterId]}')
        return CastToInt(ParsedData['inverters'][0]['AC']['0']['Power']['v'])

    def CheckMinVersion(self):
        MinVersion = 'v24.2.12'
        ParsedData = self.GetJson('/api/system/status')
        OpenDTUVersion = str((ParsedData["git_hash"]))
        if ("-Database" in OpenDTUVersion): #trim string "v24.5.27-Database"
            OpenDTUVersion = OpenDTUVersion.replace("-Database", "")
        logger.info('OpenDTU: Current Version: %s',OpenDTUVersion)
        if version.parse(OpenDTUVersion) < version.parse(MinVersion):
            logger.error('Error: Your OpenDTU Version is too old! Please update at least to Version %s - you can find the newest dev-releases here: https://github.com/tbnobody/OpenDTU/actions',MinVersion)
            quit()

    def GetAvailable(self, pInverterId: int):
        ParsedData = self.GetJson(f'/api/livedata/status?inv={SERIAL_NUMBER[pInverterId]}')
        Reachable = bool(ParsedData['inverters'][0]["reachable"])
        logger.info('OpenDTU: Inverter "%s" reachable: %s',NAME[pInverterId],Reachable)
        return Reachable

    def GetActualLimitInW(self, pInverterId: int):
        ParsedData = self.GetJson('/api/limit/status')
        limit_relative = float(ParsedData[SERIAL_NUMBER[pInverterId]]['limit_relative'])
        LimitInW = HOY_INVERTER_WATT[pInverterId] * limit_relative / 100
        return LimitInW

    def GetInfo(self, pInverterId: int):
        if SERIAL_NUMBER[pInverterId] == '':
            ParsedData = self.GetJson('/api/livedata/status')
            SERIAL_NUMBER[pInverterId] = str(ParsedData['inverters'][pInverterId]['serial'])

        ParsedData = self.GetJson(f'/api/livedata/status?inv={SERIAL_NUMBER[pInverterId]}')
        TEMPERATURE[pInverterId] = str(round(float((ParsedData['inverters'][0]['INV']['0']['Temperature']['v'])),1)) + ' degC'
        NAME[pInverterId] = str(ParsedData['inverters'][0]['name'])
        logger.info('OpenDTU: Inverter "%s" / serial number "%s" / temperature %s',NAME[pInverterId],SERIAL_NUMBER[pInverterId],TEMPERATURE[pInverterId])

    def GetTemperature(self, pInverterId: int):
        ParsedData = self.GetJson(f'/api/livedata/status?inv={SERIAL_NUMBER[pInverterId]}')
        TEMPERATURE[pInverterId] = str(round(float((ParsedData['inverters'][0]['INV']['0']['Temperature']['v'])),1)) + ' degC'
        logger.info('OpenDTU: Inverter "%s" temperature: %s',NAME[pInverterId],TEMPERATURE[pInverterId])

    def GetPanelMinVoltage(self, pInverterId: int):
        ParsedData = self.GetJson(f'/api/livedata/status?inv={SERIAL_NUMBER[pInverterId]}')
        PanelVDC = []
        ExcludedPanels = GetNumberArray(HOY_BATTERY_IGNORE_PANELS[pInverterId])
        for i in range(len(ParsedData['inverters'][0]['DC'])):
            if i not in ExcludedPanels:
                PanelVDC.append(float(ParsedData['inverters'][0]['DC'][str(i)]['Voltage']['v']))
        minVdc = float('inf')
        for i in range(len(PanelVDC)):
            if (minVdc > PanelVDC[i]) and (PanelVDC[i] > 5):
                minVdc = PanelVDC[i]
        if minVdc == float('inf'):
            minVdc = 0

        # save last 5 min-values in list and return the "highest" value.
        HOY_PANEL_VOLTAGE_LIST[pInverterId].append(minVdc)
        if len(HOY_PANEL_VOLTAGE_LIST[pInverterId]) > 5:
            HOY_PANEL_VOLTAGE_LIST[pInverterId].pop(0)
        max_value = None
        for num in HOY_PANEL_VOLTAGE_LIST[pInverterId]:
            if (max_value is None or num > max_value):
                max_value = num

        return max_value

    def WaitForAck(self, pInverterId: int, pTimeoutInS: int):
        try:
            timeout = pTimeoutInS
            timeout_start = time.time()
            while time.time() < timeout_start + timeout:
                time.sleep(0.5)
                ParsedData = self.GetJson('/api/limit/status')
                ack = (ParsedData[SERIAL_NUMBER[pInverterId]]['limit_set_status'] == 'Ok')
                if ack:
                    break
            if ack:
                logger.info('OpenDTU: Inverter "%s": Limit acknowledged', NAME[pInverterId])
            else:
                logger.info('OpenDTU: Inverter "%s": Limit timeout!', NAME[pInverterId])
            return ack
        except Exception as e:
            if hasattr(e, 'message'):
                logger.error('OpenDTU: Inverter "%s" WaitForAck: "%s"', NAME[pInverterId], e.message)
            else:
                logger.error('OpenDTU: Inverter "%s" WaitForAck: "%s"', NAME[pInverterId], e)
            return False

    def SetLimit(self, pInverterId: int, pLimit: int):
        logger.info('OpenDTU: Inverter "%s": setting new limit from %s Watt to %s Watt',NAME[pInverterId],CastToInt(CURRENT_LIMIT[pInverterId]),CastToInt(pLimit))
        relLimit = CastToInt(pLimit / HOY_INVERTER_WATT[pInverterId] * 100)
        mySendStr = f'''data={{"serial":"{SERIAL_NUMBER[pInverterId]}", "limit_type":1, "limit_value":{relLimit}}}'''
        response = self.GetResponseJson('/api/limit/config', mySendStr)
        if response['type'] != 'success':
            raise Exception(f"Error: SetLimit error: {response['message']}")
        CURRENT_LIMIT[pInverterId] = pLimit

    def SetPowerStatus(self, pInverterId: int, pActive: bool):
        if pActive:
            logger.info('OpenDTU: Inverter "%s": Turn on',NAME[pInverterId])
        else:
            logger.info('OpenDTU: Inverter "%s": Turn off',NAME[pInverterId])
        mySendStr = f'''data={{"serial":"{SERIAL_NUMBER[pInverterId]}", "power":{json.dumps(pActive)}}}'''
        response = self.GetResponseJson('/api/power/config', mySendStr)
        if response['type'] != 'success':
            raise Exception(f"Error: SetPowerStatus error: {response['message']}")

class DebugDTU(DTU):
    def __init__(self, inverter_count: int):
        super().__init__(inverter_count)

    def GetACPower(self, pInverterId):
        return CastToInt(input("Current AC-Power: "))

    def CheckMinVersion(self):
        return

    def GetAvailable(self, pInverterId: int):
        logger.info('Debug: Inverter "%s" Available: %s',NAME[pInverterId], True)
        return True

    def GetActualLimitInW(self, pInverterId: int):
        return CastToInt(input("Current InverterLimit: "))

    def GetInfo(self, pInverterId: int):
        SERIAL_NUMBER[pInverterId] = str(pInverterId)
        NAME[pInverterId] = str(pInverterId)
        TEMPERATURE[pInverterId] = '0 degC'
        logger.info('Debug: Inverter "%s" / serial number "%s" / temperature %s',NAME[pInverterId],SERIAL_NUMBER[pInverterId],TEMPERATURE[pInverterId])

    def GetTemperature(self, pInverterId: int):
        TEMPERATURE[pInverterId] = 0
        logger.info('Debug: Inverter "%s" temperature: %s',NAME[pInverterId],TEMPERATURE[pInverterId])

    def GetPanelMinVoltage(self, pInverterId: int):
        logger.info('Lowest panel voltage inverter "%s": %s Volt',NAME[pInverterId],90)
        return 90

    def WaitForAck(self, pInverterId: int, pTimeoutInS: int):
        return True

    def SetLimit(self, pInverterId: int, pLimit: int):
        logger.info('Debug: Inverter "%s": setting new limit from %s Watt to %s Watt',NAME[pInverterId],CastToInt(CURRENT_LIMIT[pInverterId]),CastToInt(pLimit))
        CURRENT_LIMIT[pInverterId] = pLimit

    def SetPowerStatus(self, pInverterId: int, pActive: bool):
        if pActive:
            logger.info('Debug: Inverter "%s": Turn on',NAME[pInverterId])
        else:
            logger.info('Debug: Inverter "%s": Turn off',NAME[pInverterId])

    def Authenticate(self):
        logger.info('Debug: Authenticating...')
        self.Token = '12345'
        logger.info('Debug: Authenticating successful, received Token: %s', self.Token)

class Script(Powermeter):
    def __init__(self, file: str, ip: str, user: str, password: str):
        self.file = file
        self.ip = ip
        self.user = user
        self.password = password

    def GetPowermeterWatts(self):
        power = subprocess.check_output([self.file, self.ip, self.user, self.password])
        return CastToInt(power)

def extract_json_value(data, path):
    from jsonpath_ng import parse
    jsonpath_expr = parse(path)
    match = jsonpath_expr.find(data)
    if match:
        return int(float(match[0].value))
    else:
        raise ValueError("No match found for the JSON path")

class MqttPowermeter(Powermeter):
    def __init__(
        self,
        broker: str,
        port: int,
        topic_incoming: str,
        json_path_incoming: str = None,
        topic_outgoing: str = None,
        json_path_outgoing: str = None,
        username: str = None,
        password: str = None,
    ):
        self.broker = broker
        self.port = port
        self.topic_incoming = topic_incoming
        self.json_path_incoming = json_path_incoming
        self.topic_outgoing = topic_outgoing
        self.json_path_outgoing = json_path_outgoing
        self.username = username
        self.password = password
        self.value_incoming = None
        self.value_outgoing = None

        # Initialize MQTT client
        import paho.mqtt.client as mqtt
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if self.username and self.password:
            self.client.username_pw_set(self.username, self.password)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message

        # Connect to the broker
        self.client.connect(self.broker, self.port)
        self.client.loop_start()

    def on_connect(self, client, userdata, flags, reason_code, properties):
        logger.info(f"Connected with result code {reason_code}")
        # Subscribe to the topics
        client.subscribe(self.topic_incoming)
        logger.info(f"Subscribed to topic {self.topic_incoming}")
        if self.topic_outgoing and self.topic_outgoing != self.topic_incoming:
            client.subscribe(self.topic_outgoing)
            logger.info(f"Subscribed to topic {self.topic_outgoing}")

    def on_message(self, client, userdata, msg):
        payload = msg.payload.decode()
        try:
            data = json.loads(payload)
            if msg.topic == self.topic_incoming:
                self.value_incoming = extract_json_value(data, self.json_path_incoming) if self.json_path_incoming else int(float(payload))
                logger.info('MQTT: Incoming power: %s Watt', self.value_incoming)
            elif msg.topic == self.topic_outgoing:
                self.value_outgoing = extract_json_value(data, self.json_path_outgoing) if self.json_path_outgoing else int(float(payload))
                logger.info('MQTT: Outgoing power: %s Watt', self.value_outgoing)
        except json.JSONDecodeError:
            print("Failed to decode JSON")

    def GetPowermeterWatts(self):
        if self.value_incoming is None:
            self.wait_for_message("incoming")
        if self.topic_outgoing and self.value_outgoing is None:
            self.wait_for_message("outgoing")

        return self.value_incoming - (self.value_outgoing if self.value_outgoing is not None else 0)

    def wait_for_message(self, message_type, timeout=5):
        start_time = time.time()
        while (message_type == "incoming" and self.value_incoming is None) or (message_type == "outgoing" and self.value_outgoing is None):
            if time.time() - start_time > timeout:
                raise TimeoutError(f"Timeout waiting for MQTT {message_type} message")
            time.sleep(1)

def CreatePowermeter() -> Powermeter:
    shelly_ip = config.get('SHELLY', 'SHELLY_IP')
    shelly_user = config.get('SHELLY', 'SHELLY_USER')
    shelly_pass = config.get('SHELLY', 'SHELLY_PASS')
    shelly_emeterindex = config.get('SHELLY', 'EMETER_INDEX')
    if config.getboolean('SELECT_POWERMETER', 'USE_SHELLY_EM'):
        return ShellyEM(shelly_ip, shelly_user, shelly_pass, shelly_emeterindex)
    elif config.getboolean('SELECT_POWERMETER', 'USE_SHELLY_3EM'):
        return Shelly3EM(shelly_ip, shelly_user, shelly_pass, shelly_emeterindex)
    elif config.getboolean('SELECT_POWERMETER', 'USE_SHELLY_3EM_PRO'):
        return Shelly3EMPro(shelly_ip, shelly_user, shelly_pass, shelly_emeterindex)
    elif config.getboolean('SELECT_POWERMETER', 'USE_TASMOTA'):
        return Tasmota(
            config.get('TASMOTA', 'TASMOTA_IP'),
            config.get('TASMOTA', 'TASMOTA_USER'),
            config.get('TASMOTA', 'TASMOTA_PASS'),
            config.get('TASMOTA', 'TASMOTA_JSON_STATUS'),
            config.get('TASMOTA', 'TASMOTA_JSON_PAYLOAD_MQTT_PREFIX'),
            config.get('TASMOTA', 'TASMOTA_JSON_POWER_MQTT_LABEL'),
            config.get('TASMOTA', 'TASMOTA_JSON_POWER_INPUT_MQTT_LABEL'),
            config.get('TASMOTA', 'TASMOTA_JSON_POWER_OUTPUT_MQTT_LABEL'),
            config.getboolean('TASMOTA', 'TASMOTA_JSON_POWER_CALCULATE', fallback=False)
        )
    elif config.getboolean('SELECT_POWERMETER', 'USE_SHRDZM'):
        return Shrdzm(
            config.get('SHRDZM', 'SHRDZM_IP'),
            config.get('SHRDZM', 'SHRDZM_USER'),
            config.get('SHRDZM', 'SHRDZM_PASS')
        )
    elif config.getboolean('SELECT_POWERMETER', 'USE_EMLOG'):
        return Emlog(
            config.get('EMLOG', 'EMLOG_IP'),
            config.get('EMLOG', 'EMLOG_METERINDEX'),
            config.getboolean('EMLOG', 'EMLOG_JSON_POWER_CALCULATE', fallback=False)
        )
    elif config.getboolean('SELECT_POWERMETER', 'USE_IOBROKER'):
        return IoBroker(
            config.get('IOBROKER', 'IOBROKER_IP'),
            config.get('IOBROKER', 'IOBROKER_PORT'),
            config.get('IOBROKER', 'IOBROKER_CURRENT_POWER_ALIAS'),
            config.getboolean('IOBROKER', 'IOBROKER_POWER_CALCULATE'),
            config.get('IOBROKER', 'IOBROKER_POWER_INPUT_ALIAS'),
            config.get('IOBROKER', 'IOBROKER_POWER_OUTPUT_ALIAS')
        )
    elif config.getboolean('SELECT_POWERMETER', 'USE_HOMEASSISTANT'):
        return HomeAssistant(
            config.get('HOMEASSISTANT', 'HA_IP'),
            config.get('HOMEASSISTANT', 'HA_PORT'),
            config.getboolean('HOMEASSISTANT', 'HA_HTTPS', fallback=False),
            config.get('HOMEASSISTANT', 'HA_ACCESSTOKEN'),
            config.get('HOMEASSISTANT', 'HA_CURRENT_POWER_ENTITY'),
            config.getboolean('HOMEASSISTANT', 'HA_POWER_CALCULATE'),
            config.get('HOMEASSISTANT', 'HA_POWER_INPUT_ALIAS'),
            config.get('HOMEASSISTANT', 'HA_POWER_OUTPUT_ALIAS')
        )
    elif config.getboolean('SELECT_POWERMETER', 'USE_VZLOGGER'):
        return VZLogger(
            config.get('VZLOGGER', 'VZL_IP'),
            config.get('VZLOGGER', 'VZL_PORT'),
            config.get('VZLOGGER', 'VZL_UUID')
        )
    elif config.getboolean('SELECT_POWERMETER', 'USE_SCRIPT'):
        return Script(
            config.get('SCRIPT', 'SCRIPT_FILE'),
            config.get('SCRIPT', 'SCRIPT_IP'),
            config.get('SCRIPT', 'SCRIPT_USER'),
            config.get('SCRIPT', 'SCRIPT_PASS')
        )
    elif config.getboolean('SELECT_POWERMETER', 'USE_AMIS_READER'):
        return AmisReader(
            config.get('AMIS_READER', 'AMIS_READER_IP')
        )
    elif config.getboolean('SELECT_POWERMETER', 'USE_MQTT'):
        return MqttPowermeter(
            config.get('MQTT_POWERMETER', 'MQTT_BROKER', fallback=config.get("MQTT_CONFIG", "MQTT_BROKER", fallback=None)),
            config.getint('MQTT_POWERMETER', 'MQTT_PORT', fallback=config.getint("MQTT_CONFIG", "MQTT_PORT", fallback=1883)),
            config.get('MQTT_POWERMETER', 'MQTT_TOPIC_INCOMING'),
            config.get('MQTT_POWERMETER', 'MQTT_JSON_PATH_INCOMING', fallback=None),
            config.get('MQTT_POWERMETER', 'MQTT_TOPIC_OUTGOING', fallback=None),
            config.get('MQTT_POWERMETER', 'MQTT_JSON_PATH_OUTGOING', fallback=None),
            config.get('MQTT_POWERMETER', 'MQTT_USERNAME', fallback=config.get('MQTT_CONFIG', 'MQTT_USERNAME', fallback=None)),
            config.get('MQTT_POWERMETER', 'MQTT_PASSWORD', fallback=config.get('MQTT_CONFIG', 'MQTT_PASSWORD', fallback=None))
        )
    elif config.getboolean('SELECT_POWERMETER', 'USE_MODBUS_TCP'):
        return ModbusTCP(
            config.get("MODBUS_TCP", "MODBUS_TCP_IP"),
            config.getint("MODBUS_TCP", "MODBUS_TCP_UNIT_ID"),
            config.getint("MODBUS_TCP", "MODBUS_TCP_REGISTER"),
            config.get("MODBUS_TCP", "MODBUS_TCP_REGISTER_TYPE"),
            config.getfloat("MODBUS_TCP", "MODBUS_TCP_REGISTER_SCALE")
        )
    elif config.getboolean('SELECT_POWERMETER', 'USE_DEBUG_READER'):
        return DebugReader()
    else:
        raise Exception("Error: no powermeter defined!")

def CreateIntermediatePowermeter(dtu: DTU) -> Powermeter:
    shelly_ip = config.get('INTERMEDIATE_SHELLY', 'SHELLY_IP_INTERMEDIATE')
    shelly_user = config.get('INTERMEDIATE_SHELLY', 'SHELLY_USER_INTERMEDIATE')
    shelly_pass = config.get('INTERMEDIATE_SHELLY', 'SHELLY_PASS_INTERMEDIATE')
    shelly_emeterindex = config.get('INTERMEDIATE_SHELLY', 'EMETER_INDEX')
    if config.getboolean('SELECT_INTERMEDIATE_METER', 'USE_TASMOTA_INTERMEDIATE'):
        return Tasmota(
            config.get('INTERMEDIATE_TASMOTA', 'TASMOTA_IP_INTERMEDIATE'),
            config.get('INTERMEDIATE_TASMOTA', 'TASMOTA_USER_INTERMEDIATE'),
            config.get('INTERMEDIATE_TASMOTA', 'TASMOTA_PASS_INTERMEDIATE'),
            config.get('INTERMEDIATE_TASMOTA', 'TASMOTA_JSON_STATUS_INTERMEDIATE'),
            config.get('INTERMEDIATE_TASMOTA', 'TASMOTA_JSON_PAYLOAD_MQTT_PREFIX_INTERMEDIATE'),
            config.get('INTERMEDIATE_TASMOTA', 'TASMOTA_JSON_POWER_MQTT_LABEL_INTERMEDIATE'),
            config.get('INTERMEDIATE_TASMOTA', 'TASMOTA_JSON_POWER_INPUT_MQTT_LABEL_INTERMEDIATE', fallback=None),
            config.get('INTERMEDIATE_TASMOTA', 'TASMOTA_JSON_POWER_OUTPUT_MQTT_LABEL_INTERMEDIATE', fallback=None),
            config.getboolean('INTERMEDIATE_TASMOTA', 'TASMOTA_JSON_POWER_CALCULATE_INTERMEDIATE', fallback=False)
        )
    elif config.getboolean('SELECT_INTERMEDIATE_METER', 'USE_SHELLY_EM_INTERMEDIATE'):
        return ShellyEM(shelly_ip, shelly_user, shelly_pass, shelly_emeterindex)
    elif config.getboolean('SELECT_INTERMEDIATE_METER', 'USE_SHELLY_3EM_INTERMEDIATE'):
        return Shelly3EM(shelly_ip, shelly_user, shelly_pass, shelly_emeterindex)
    elif config.getboolean('SELECT_INTERMEDIATE_METER', 'USE_SHELLY_3EM_PRO_INTERMEDIATE'):
        return Shelly3EMPro(shelly_ip, shelly_user, shelly_pass, shelly_emeterindex)
    elif config.getboolean('SELECT_INTERMEDIATE_METER', 'USE_SHELLY_1PM_INTERMEDIATE'):
        return Shelly1PM(shelly_ip, shelly_user, shelly_pass, shelly_emeterindex)
    elif config.getboolean('SELECT_INTERMEDIATE_METER', 'USE_SHELLY_PLUS_1PM_INTERMEDIATE'):
        return ShellyPlus1PM(shelly_ip, shelly_user, shelly_pass, shelly_emeterindex)
    elif config.getboolean('SELECT_INTERMEDIATE_METER', 'USE_ESPHOME_INTERMEDIATE'):
        return ESPHome(
            config.get('INTERMEDIATE_ESPHOME', 'ESPHOME_IP_INTERMEDIATE'),
            config.get('INTERMEDIATE_ESPHOME', 'ESPHOME_PORT_INTERMEDIATE', fallback='80'),
            config.get('INTERMEDIATE_ESPHOME', 'ESPHOME_DOMAIN_INTERMEDIATE'),
            config.get('INTERMEDIATE_ESPHOME', 'ESPHOME_ID_INTERMEDIATE')
        )
    elif config.getboolean('SELECT_INTERMEDIATE_METER', 'USE_SHRDZM_INTERMEDIATE'):
        return Shrdzm(
            config.get('INTERMEDIATE_SHRDZM', 'SHRDZM_IP_INTERMEDIATE'),
            config.get('INTERMEDIATE_SHRDZM', 'SHRDZM_USER_INTERMEDIATE'),
            config.get('INTERMEDIATE_SHRDZM', 'SHRDZM_PASS_INTERMEDIATE')
        )
    elif config.getboolean('SELECT_INTERMEDIATE_METER', 'USE_EMLOG_INTERMEDIATE'):
        return Emlog(
            config.get('INTERMEDIATE_EMLOG', 'EMLOG_IP_INTERMEDIATE'),
            config.get('INTERMEDIATE_EMLOG', 'EMLOG_METERINDEX_INTERMEDIATE'),
            config.getboolean('INTERMEDIATE_EMLOG', 'EMLOG_JSON_POWER_CALCULATE', fallback=False)
        )
    elif config.getboolean('SELECT_INTERMEDIATE_METER', 'USE_IOBROKER_INTERMEDIATE'):
        return IoBroker(
            config.get('INTERMEDIATE_IOBROKER', 'IOBROKER_IP_INTERMEDIATE'),
            config.get('INTERMEDIATE_IOBROKER', 'IOBROKER_PORT_INTERMEDIATE'),
            config.get('INTERMEDIATE_IOBROKER', 'IOBROKER_CURRENT_POWER_ALIAS_INTERMEDIATE'),
            config.getboolean('INTERMEDIATE_IOBROKER', 'IOBROKER_POWER_CALCULATE', fallback=False),
            config.get('INTERMEDIATE_IOBROKER', 'IOBROKER_POWER_INPUT_ALIAS_INTERMEDIATE', fallback=None),
            config.get('INTERMEDIATE_IOBROKER', 'IOBROKER_POWER_OUTPUT_ALIAS_INTERMEDIATE', fallback=None)
        )
    elif config.getboolean('SELECT_INTERMEDIATE_METER', 'USE_HOMEASSISTANT_INTERMEDIATE'):
        return HomeAssistant(
            config.get('INTERMEDIATE_HOMEASSISTANT', 'HA_IP_INTERMEDIATE'),
            config.get('INTERMEDIATE_HOMEASSISTANT', 'HA_PORT_INTERMEDIATE'),
            config.getboolean('INTERMEDIATE_HOMEASSISTANT', 'HA_HTTPS_INTERMEDIATE', fallback=False),
            config.get('INTERMEDIATE_HOMEASSISTANT', 'HA_ACCESSTOKEN_INTERMEDIATE'),
            config.get('INTERMEDIATE_HOMEASSISTANT', 'HA_CURRENT_POWER_ENTITY_INTERMEDIATE'),
            config.getboolean('INTERMEDIATE_HOMEASSISTANT', 'HA_POWER_CALCULATE_INTERMEDIATE', fallback=False),
            config.get('INTERMEDIATE_HOMEASSISTANT', 'HA_POWER_INPUT_ALIAS_INTERMEDIATE', fallback=None),
            config.get('INTERMEDIATE_HOMEASSISTANT', 'HA_POWER_OUTPUT_ALIAS_INTERMEDIATE', fallback=None)
        )
    elif config.getboolean('SELECT_INTERMEDIATE_METER', 'USE_VZLOGGER_INTERMEDIATE'):
        return VZLogger(
            config.get('INTERMEDIATE_VZLOGGER', 'VZL_IP_INTERMEDIATE'),
            config.get('INTERMEDIATE_VZLOGGER', 'VZL_PORT_INTERMEDIATE'),
            config.get('INTERMEDIATE_VZLOGGER', 'VZL_UUID_INTERMEDIATE')
        )
    elif config.getboolean('SELECT_INTERMEDIATE_METER', 'USE_SCRIPT_INTERMEDIATE'):
        return Script(
            config.get('INTERMEDIATE_SCRIPT', 'SCRIPT_FILE_INTERMEDIATE'),
            config.get('INTERMEDIATE_SCRIPT', 'SCRIPT_IP_INTERMEDIATE'),
            config.get('INTERMEDIATE_SCRIPT', 'SCRIPT_USER_INTERMEDIATE'),
            config.get('INTERMEDIATE_SCRIPT', 'SCRIPT_PASS_INTERMEDIATE')
        )
    elif config.getboolean('SELECT_INTERMEDIATE_METER', 'USE_MQTT_INTERMEDIATE'):
        return MqttPowermeter(
            config.get('INTERMEDIATE_MQTT', 'MQTT_BROKER', fallback=config.get("MQTT_CONFIG", "MQTT_BROKER", fallback=None)),
            config.getint('INTERMEDIATE_MQTT', 'MQTT_PORT', fallback=config.getint("MQTT_CONFIG", "MQTT_PORT", fallback=1883)),
            config.get('INTERMEDIATE_MQTT', 'MQTT_TOPIC_INCOMING'),
            config.get('INTERMEDIATE_MQTT', 'MQTT_JSON_PATH_INCOMING', fallback=None),
            config.get('INTERMEDIATE_MQTT', 'MQTT_TOPIC_OUTGOING', fallback=None),
            config.get('INTERMEDIATE_MQTT', 'MQTT_JSON_PATH_OUTGOING', fallback=None),
            config.get('INTERMEDIATE_MQTT', 'MQTT_USERNAME', fallback=config.get("MQTT_CONFIG", "MQTT_USERNAME", fallback=None)),
            config.get('INTERMEDIATE_MQTT', 'MQTT_PASSWORD', fallback=config.get("MQTT_CONFIG", "MQTT_PASSWORD", fallback=None))
        )
    elif config.getboolean('SELECT_INTERMEDIATE_METER', 'USE_AMIS_READER_INTERMEDIATE'):
        return AmisReader(
            config.get('INTERMEDIATE_AMIS_READER', 'AMIS_READER_IP_INTERMEDIATE')
        )
    elif config.getboolean('SELECT_INTERMEDIATE_METER', 'USE_DEBUG_READER_INTERMEDIATE'):
        return DebugReader()
    else:
        return dtu

def CreateDTU() -> DTU:
    inverter_count = config.getint('COMMON', 'INVERTER_COUNT')
    if config.getboolean('SELECT_DTU', 'USE_AHOY'):
        return AhoyDTU(
            inverter_count,
            config.get('AHOY_DTU', 'AHOY_IP'),
            config.get('AHOY_DTU', 'AHOY_PASS', fallback='')
        )
    elif config.getboolean('SELECT_DTU', 'USE_OPENDTU'):
        return OpenDTU(
            inverter_count,
            config.get('OPEN_DTU', 'OPENDTU_IP'),
            config.get('OPEN_DTU', 'OPENDTU_USER'),
            config.get('OPEN_DTU', 'OPENDTU_PASS')
        )
    elif config.getboolean('SELECT_DTU', 'USE_DEBUG'):
        return DebugDTU(
            inverter_count
        )
    else:
        raise Exception("Error: no DTU defined!")

# ----- START -----
logger.info("Author: %s / Script Version: %s",__author__, __version__)

# read config:
logger.info("read config file: " + str(Path.joinpath(Path(__file__).parent.resolve(), "HoymilesZeroExport_Config.ini")))
if args.config:
    logger.info("read additional config file: " + args.config)

VERSION = config.get('VERSION', 'VERSION')
logger.info("Config file V %s", VERSION)

MAX_RETRIES = config.getint('COMMON', 'MAX_RETRIES', fallback=3)
RETRY_STATUS_CODES = config.get('COMMON', 'RETRY_STATUS_CODES', fallback='500,502,503,504')
RETRY_BACKOFF_FACTOR = config.getfloat('COMMON', 'RETRY_BACKOFF_FACTOR', fallback=0.1)
retry = Retry(total=MAX_RETRIES,
              backoff_factor=RETRY_BACKOFF_FACTOR,
              status_forcelist=[int(status_code) for status_code in RETRY_STATUS_CODES.split(',')],
              allowed_methods={"GET", "POST"})
adapter = HTTPAdapter(max_retries=retry)
session.mount('http://', adapter)
session.mount('https://', adapter)

USE_AHOY = config.getboolean('SELECT_DTU', 'USE_AHOY')
USE_OPENDTU = config.getboolean('SELECT_DTU', 'USE_OPENDTU')
AHOY_IP = config.get('AHOY_DTU', 'AHOY_IP')
OPENDTU_IP = config.get('OPEN_DTU', 'OPENDTU_IP')
OPENDTU_USER = config.get('OPEN_DTU', 'OPENDTU_USER')
OPENDTU_PASS = config.get('OPEN_DTU', 'OPENDTU_PASS')
DTU = CreateDTU()
POWERMETER = CreatePowermeter()
INTERMEDIATE_POWERMETER = CreateIntermediatePowermeter(DTU)
INVERTER_COUNT = config.getint('COMMON', 'INVERTER_COUNT')
LOOP_INTERVAL_IN_SECONDS = config.getint('COMMON', 'LOOP_INTERVAL_IN_SECONDS')
SET_LIMIT_TIMEOUT_SECONDS = config.getint('COMMON', 'SET_LIMIT_TIMEOUT_SECONDS')
SET_POWER_STATUS_DELAY_IN_SECONDS = config.getint('COMMON', 'SET_POWER_STATUS_DELAY_IN_SECONDS')
POLL_INTERVAL_IN_SECONDS = config.getint('COMMON', 'POLL_INTERVAL_IN_SECONDS')
MAX_DIFFERENCE_BETWEEN_LIMIT_AND_OUTPUTPOWER = config.getint('COMMON', 'MAX_DIFFERENCE_BETWEEN_LIMIT_AND_OUTPUTPOWER')
SET_POWERSTATUS_CNT = config.getint('COMMON', 'SET_POWERSTATUS_CNT')
SLOW_APPROX_FACTOR_IN_PERCENT = config.getint('COMMON', 'SLOW_APPROX_FACTOR_IN_PERCENT')
LOG_TEMPERATURE = config.getboolean('COMMON', 'LOG_TEMPERATURE')
SET_INVERTER_TO_MIN_ON_POWERMETER_ERROR = config.getboolean('COMMON', 'SET_INVERTER_TO_MIN_ON_POWERMETER_ERROR', fallback=False)
powermeter_target_point = config.getint('CONTROL', 'POWERMETER_TARGET_POINT')
SERIAL_NUMBER = []
ENABLED = []
NAME = []
TEMPERATURE = []
HOY_MAX_WATT = []
HOY_INVERTER_WATT = []
CURRENT_LIMIT = []
AVAILABLE = []
LASTLIMITACKNOWLEDGED = []
HOY_BATTERY_GOOD_VOLTAGE = []
HOY_COMPENSATE_WATT_FACTOR = []
HOY_BATTERY_MODE = []
HOY_BATTERY_THRESHOLD_OFF_LIMIT_IN_V = []
HOY_BATTERY_THRESHOLD_REDUCE_LIMIT_IN_V = []
HOY_BATTERY_THRESHOLD_NORMAL_LIMIT_IN_V = []
HOY_BATTERY_THRESHOLD_ON_LIMIT_IN_V = []
HOY_BATTERY_IGNORE_PANELS = []
HOY_PANEL_VOLTAGE_LIST = []
HOY_PANEL_MIN_VOLTAGE_HISTORY_LIST = []
HOY_BATTERY_AVERAGE_CNT = []
for i in range(INVERTER_COUNT):
    SERIAL_NUMBER.append(config.get('INVERTER_' + str(i + 1), 'SERIAL_NUMBER', fallback=''))
    ENABLED.append(config.getboolean('INVERTER_' + str(i + 1), 'ENABLED', fallback = True))
    NAME.append(str('yet unknown'))
    TEMPERATURE.append(str('--- degC'))
    HOY_MAX_WATT.append(config.getint('INVERTER_' + str(i + 1), 'HOY_MAX_WATT'))

    if (config.get('INVERTER_' + str(i + 1), 'HOY_INVERTER_WATT') != ''):
        HOY_INVERTER_WATT.append(config.getint('INVERTER_' + str(i + 1), 'HOY_INVERTER_WATT'))
    else:
        HOY_INVERTER_WATT.append(HOY_MAX_WATT[i])

    CURRENT_LIMIT.append(int(-1))
    AVAILABLE.append(bool(False))
    LASTLIMITACKNOWLEDGED.append(bool(False))
    HOY_BATTERY_GOOD_VOLTAGE.append(bool(True))
    HOY_BATTERY_MODE.append(config.getboolean('INVERTER_' + str(i + 1), 'HOY_BATTERY_MODE'))
    HOY_BATTERY_THRESHOLD_OFF_LIMIT_IN_V.append(config.getfloat('INVERTER_' + str(i + 1), 'HOY_BATTERY_THRESHOLD_OFF_LIMIT_IN_V'))
    HOY_BATTERY_THRESHOLD_REDUCE_LIMIT_IN_V.append(config.getfloat('INVERTER_' + str(i + 1), 'HOY_BATTERY_THRESHOLD_REDUCE_LIMIT_IN_V'))
    HOY_BATTERY_THRESHOLD_NORMAL_LIMIT_IN_V.append(config.getfloat('INVERTER_' + str(i + 1), 'HOY_BATTERY_THRESHOLD_NORMAL_LIMIT_IN_V'))
    HOY_BATTERY_THRESHOLD_ON_LIMIT_IN_V.append(config.getfloat('INVERTER_' + str(i + 1), 'HOY_BATTERY_THRESHOLD_ON_LIMIT_IN_V'))
    HOY_COMPENSATE_WATT_FACTOR.append(config.getfloat('INVERTER_' + str(i + 1), 'HOY_COMPENSATE_WATT_FACTOR'))
    HOY_BATTERY_IGNORE_PANELS.append(config.get('INVERTER_' + str(i + 1), 'HOY_BATTERY_IGNORE_PANELS'))
    HOY_PANEL_VOLTAGE_LIST.append([])
    HOY_PANEL_MIN_VOLTAGE_HISTORY_LIST.append([])
    HOY_BATTERY_AVERAGE_CNT.append(config.getint('INVERTER_' + str(i + 1), 'HOY_BATTERY_AVERAGE_CNT', fallback=1))
SLOW_APPROX_LIMIT = CastToInt(GetMaxWattFromAllInverters() * config.getint('COMMON', 'SLOW_APPROX_LIMIT_IN_PERCENT') / 100)

CONFIG_PROVIDER = ConfigFileConfigProvider(config)
MQTT = None
if config.has_section("MQTT_CONFIG"):
    broker = config.get("MQTT_CONFIG", "MQTT_BROKER")
    port = config.getint("MQTT_CONFIG", "MQTT_PORT", fallback=1883)
    client_id = config.get("MQTT_CONFIG", "MQTT_CLIENT_ID", fallback="HoymilesZeroExport")
    username = config.get("MQTT_CONFIG", "MQTT_USERNAME", fallback=None)
    password = config.get("MQTT_CONFIG", "MQTT_PASSWORD", fallback=None)
    topic_prefix = config.get("MQTT_CONFIG", "MQTT_SET_TOPIC", fallback="zeropower")
    log_level_config_value = config.get("MQTT_CONFIG", "MQTT_LOG_LEVEL", fallback=None)
    mqtt_log_level = logging.getLevelName(log_level_config_value) if log_level_config_value else None
    MQTT = MqttHandler(broker, port, client_id, username, password, topic_prefix, mqtt_log_level)

    if mqtt_log_level is not None:
        class MqttLogHandler(logging.Handler):
            def emit(self, record):
                MQTT.publish_log_record(record)

        logger.addHandler(MqttLogHandler())

    CONFIG_PROVIDER = ConfigProviderChain([MQTT, CONFIG_PROVIDER])

try:
    logger.info("---Init---")
    newLimitSetpoint = 0
    DTU.CheckMinVersion()
    if GetHoymilesAvailable():
        for i in range(INVERTER_COUNT):
            SetHoymilesPowerStatus(i, True)
        newLimitSetpoint = GetMinWattFromAllInverters()
        SetLimit(newLimitSetpoint)
        GetHoymilesActualPower()
        GetCheckBattery()
    GetPowermeterWatts()
except Exception as e:
    if hasattr(e, "message"):
        logger.error(e.message)
    else:
        logger.error(e)
    time.sleep(LOOP_INTERVAL_IN_SECONDS)
logger.info("---Start Zero Export---")

while True:
    CONFIG_PROVIDER.update()
    PublishConfigState()
    on_grid_usage_jump_to_limit_percent = CONFIG_PROVIDER.on_grid_usage_jump_to_limit_percent()
    on_grid_feed_fast_limit_decrease = CONFIG_PROVIDER.on_grid_feed_fast_limit_decrease()
    powermeter_target_point = CONFIG_PROVIDER.get_powermeter_target_point()
    powermeter_max_point = CONFIG_PROVIDER.get_powermeter_max_point()
    powermeter_min_point = CONFIG_PROVIDER.get_powermeter_min_point()
    powermeter_tolerance = CONFIG_PROVIDER.get_powermeter_tolerance()
    if powermeter_max_point < (powermeter_target_point + powermeter_tolerance):
        powermeter_max_point = powermeter_target_point + powermeter_tolerance + 50
        logger.info(
            'Warning: POWERMETER_MAX_POINT < POWERMETER_TARGET_POINT + POWERMETER_TOLERANCE. Setting POWERMETER_MAX_POINT to ' + str(
                powermeter_max_point))

    try:
        PreviousLimitSetpoint = newLimitSetpoint
        if GetHoymilesAvailable() and GetCheckBattery():
            if LOG_TEMPERATURE:
                GetHoymilesTemperature()
            for x in range(
                CastToInt(LOOP_INTERVAL_IN_SECONDS / POLL_INTERVAL_IN_SECONDS)
            ):
                powermeterWatts = GetPowermeterWatts()
                if powermeterWatts > powermeter_max_point:
                    if on_grid_usage_jump_to_limit_percent > 0:
                        newLimitSetpoint = CastToInt(GetMaxInverterWattFromAllInverters() * on_grid_usage_jump_to_limit_percent / 100)
                        if (newLimitSetpoint <= PreviousLimitSetpoint) and (on_grid_usage_jump_to_limit_percent != 100):
                            newLimitSetpoint = PreviousLimitSetpoint + powermeterWatts - powermeter_target_point
                    else:
                        newLimitSetpoint = PreviousLimitSetpoint + powermeterWatts - powermeter_target_point
                    newLimitSetpoint = ApplyLimitsToSetpoint(newLimitSetpoint)
                    SetLimit(newLimitSetpoint)
                    RemainingDelay = CastToInt((LOOP_INTERVAL_IN_SECONDS / POLL_INTERVAL_IN_SECONDS - x) * POLL_INTERVAL_IN_SECONDS)
                    if RemainingDelay > 0:
                        time.sleep(RemainingDelay)
                        break
                elif (powermeterWatts < powermeter_min_point) and on_grid_feed_fast_limit_decrease:
                    newLimitSetpoint = PreviousLimitSetpoint + powermeterWatts - powermeter_target_point
                    newLimitSetpoint = ApplyLimitsToSetpoint(newLimitSetpoint)
                    SetLimit(newLimitSetpoint)
                    RemainingDelay = CastToInt((LOOP_INTERVAL_IN_SECONDS / POLL_INTERVAL_IN_SECONDS - x) * POLL_INTERVAL_IN_SECONDS)
                    if RemainingDelay > 0:
                        time.sleep(RemainingDelay)
                        break
                else:
                    time.sleep(POLL_INTERVAL_IN_SECONDS)

            if MAX_DIFFERENCE_BETWEEN_LIMIT_AND_OUTPUTPOWER != 100:
                CutLimit = CutLimitToProduction(newLimitSetpoint)
                if CutLimit != newLimitSetpoint:
                    newLimitSetpoint = CutLimit
                    PreviousLimitSetpoint = newLimitSetpoint

            if powermeterWatts > powermeter_max_point:
                continue

            # producing too much power: reduce limit
            if powermeterWatts < (powermeter_target_point - powermeter_tolerance):
                if PreviousLimitSetpoint >= GetMaxWattFromAllInverters():
                    hoymilesActualPower = GetHoymilesActualPower()
                    newLimitSetpoint = hoymilesActualPower + powermeterWatts - powermeter_target_point
                    LimitDifference = abs(hoymilesActualPower - newLimitSetpoint)
                    if LimitDifference > SLOW_APPROX_LIMIT:
                        newLimitSetpoint = newLimitSetpoint + (
                            LimitDifference * SLOW_APPROX_FACTOR_IN_PERCENT / 100
                        )
                    if newLimitSetpoint > hoymilesActualPower:
                        newLimitSetpoint = hoymilesActualPower
                    logger.info("overproducing: reduce limit based on actual power")
                else:
                    newLimitSetpoint = PreviousLimitSetpoint + powermeterWatts - powermeter_target_point
                    # check if it is necessary to approximate to the setpoint with some more passes. this reduce overshoot
                    LimitDifference = abs(PreviousLimitSetpoint - newLimitSetpoint)
                    if LimitDifference > SLOW_APPROX_LIMIT:
                        logger.info(
                            "overproducing: reduce limit based on previous limit setpoint by approximation"
                        )
                        newLimitSetpoint = newLimitSetpoint + (
                            LimitDifference * SLOW_APPROX_FACTOR_IN_PERCENT / 100
                        )
                    else:
                        logger.info(
                            "overproducing: reduce limit based on previous limit setpoint"
                        )

            # producing too little power: increase limit
            elif powermeterWatts > (powermeter_target_point + powermeter_tolerance):
                if PreviousLimitSetpoint < GetMaxWattFromAllInverters():
                    newLimitSetpoint = PreviousLimitSetpoint + powermeterWatts - powermeter_target_point
                    logger.info("Not enough energy producing: increasing limit")
                else:
                    logger.info("Not enough energy producing: limit already at maximum")

            # check for upper and lower limits
            newLimitSetpoint = ApplyLimitsToSetpoint(newLimitSetpoint)
            # set new limit to inverter
            SetLimit(newLimitSetpoint)
        else:
            if hasattr(SetLimit, "LastLimit"):
                SetLimit.LastLimit = -1
            time.sleep(LOOP_INTERVAL_IN_SECONDS)

    except Exception as e:
        if hasattr(e, "message"):
            logger.error(e.message)
        else:
            logger.error(e)
        time.sleep(LOOP_INTERVAL_IN_SECONDS)
