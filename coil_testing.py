import pyvisa
import time
import threading
import queue
import csv
import nidaqmx
from nidaqmx.constants import TerminalConfiguration, AcquisitionType
from datetime import datetime
from pyvisa.constants import StopBits, Parity
from lakeshore import Model224, Model224CurveHeader
from lakeshore.model_224 import Model224InputSensorSettings

# InfluxDB
import os
from dotenv import load_dotenv
import influxdb_client
from influxdb_client import InfluxDBClient
from influxdb_client.client.write_api import SYNCHRONOUS
load_dotenv(".env", override=True)
INFLUX_URL = os.getenv("INFLUX_URL")
INFLUX_ORG = os.getenv("INFLUX_ORG")
INFLUX_BUCKET_NAME = os.getenv("INFLUX_BUCKET_NAME")
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN")

prefix = input("Enter CSV file prefix: ").strip()
csv_filename = f"{prefix}.csv"
keithley_resource = "ASRL3::INSTR"
psu_resource = "TCPIP0::192.168.0.201::inst0::INSTR"  # IP Address TDK
psu_current_max = 550  # Safety Limit
rm = pyvisa.ResourceManager()

# Setup Comms for Keithley 2182A
keithley = rm.open_resource(keithley_resource, read_termination="\r", write_termination="\r")
keithley.baud_rate = 19200
keithley.data_bits = 8
keithley.stop_bits = StopBits.one
keithley.parity = Parity.none
keithley.timeout = 5000

# Setup Comms for TDK Lambda
psu = rm.open_resource(psu_resource, read_termination="\n", write_termination="\n")
psu.timeout = 5000

#Setup for NIDAQ for Hall probe
task = nidaqmx.Task()
task.ai_channels.add_ai_voltage_chan(
    physical_channel="cDAQ1Mod1/ai0",
    terminal_config=TerminalConfiguration.DEFAULT,
    min_val=-0.2,
    max_val=0.2
)

# Configure timing for 1 Hz sampling (1 sample per second)
task.timing.cfg_samp_clk_timing(
        rate=1.0,
        sample_mode=AcquisitionType.CONTINUOUS,
        samps_per_chan=1
    )

#Setup for NIDAQ for measuring PSU Voltage
task.ai_channels.add_ai_voltage_chan(
    physical_channel="cDAQ1Mod1/ai1",
    terminal_config=TerminalConfiguration.DEFAULT,
    min_val=-5,
    max_val=5
)

# Configure timing for 1 Hz sampling (1 sample per second)
task.timing.cfg_samp_clk_timing(
        rate=1.0,
        sample_mode=AcquisitionType.CONTINUOUS,
        samps_per_chan=1
    )
# --- Instrument Initialisation ---
start_dt = datetime.now().isoformat(sep=" ")

#Lakeshore 224 Setup
# lakeshore224 = Model224(ip_address="192.168.1.20", tcp_port=7777, timeout=5000)
# print(lakeshore224.query('*IDN?'))

# lakeshore224.set_input_curve("A", 7)
# lakeshore224.set_input_curve("B", 7)

# Check Keighley 2182A health
idn = keithley.query("*IDN?")
print("Keithley:", idn.strip())
keithley.write("*CLS")
keithley.write("*RST")
error = keithley.query("SYST:ERR?")
print("Keithley Error status:", error.strip())
# for chan in [1, 2]:
for chan in [1]:
    keithley.write(f":SENS:CHAN {chan}")
    keithley.write(":SENS:FUNC 'VOLT'")
    keithley.write(":TRIG:SOUR IMM")
    keithley.write(":TRIG:COUN INF")  # Infinite count for continuous
    keithley.write(":DISPlay:ENABle ON")
    keithley.write(f":SENS:VOLT:DC:CHAN{chan}:RANGe 0.001")
    keithley.write(f":SENS:VOLT:CHAN{chan}:NPLC 2")
    keithley.write(f":SENS:VOLT:CHAN{chan}:RANG:AUTO ON")
    keithley.write(":SYST:AZER:STATe OFF")
    keithley.write(f":SENS:VOLT:DC:CHAN{chan}:LPAS OFF")  # Fixed: Set to OFF (change to ON if needed)
    keithley.write(f":SENS:VOLT:DC:CHAN{chan}:DFIL ON")  # Fixed: Set to OFF (change to ON if needed)

# Check TDK Lambda PSU health
psu_idn = psu.query("*IDN?")
print("PSU:", psu_idn.strip())
psu.write("*CLS")
psu_err = psu.query("SYST:ERR?")
print("PSU Error status:", psu_err.strip())

# ----- keithley functions ------------------
def read_voltage(channel):
    time.sleep(0.1)
    keithley.write(f":SENS:CHAN {channel}")
    return keithley.query(":READ?").strip()

# ------- TDK Lambda Functions ------------------------
def check_opc():
    """Wait for operation complete."""
    while True:
        result = psu.query("*OPC?")
        if result.strip() == "1":
            break

def check_error():
    """Check for SCPI error and raise exception if found."""
    err = psu.query("SYST:ERR?")
    if not err.startswith("0"):
        raise RuntimeError(f"SCPI error: {err.strip()}")

def read_psu_current():
    current_str = psu.query("MEAS:CURR?").strip()
    check_opc()
    check_error()
    try:
        return float(current_str)
    except Exception:
        return None

def read_psu_voltage():
    voltage_str = psu.query("SOUR:VOLT?").strip()
    check_opc()
    check_error()
    try:
        return float(voltage_str)
    except Exception:
        return None
    
def set_psu_voltage(voltage):
    prot_voltage = float(voltage) + 1
    psu.write(f"SOUR:VOLT:PROT:LEV {prot_voltage}")
    check_opc()
    check_error()
    psu.write(f"SOUR:VOLT {voltage}")
    check_opc()
    check_error()

def set_psu_current(current):
    if float(current) >= psu_current_max:
        print("Entered current value higher than program allows.")
    else:
        psu.write(f"SOUR:CURR {current}")
        check_opc()
        check_error()
        # psu.write("*SAV 1")
        # check_opc()
        # check_error()

def set_psu_output(on=True):
    cmd = "ON" if on else "OFF"
    psu.write(f"OUTP:STAT {cmd}")
    check_opc()
    check_error()

def read_psu_output():
    val =psu.query("OUTP:STAT?")
    check_opc()
    check_error()
    return float(val.strip())

def psu_init():
    psu.write("*RST")
    set_psu_voltage(0.4)
    set_psu_current(0.0)
    set_psu_output(True)

def set_psu_ramp(current, rate):
    if float(current) >= psu_current_max:
        print("Entered current value higher than program allows.")
    else:
        set_current = float(psu.query("SOUR:CURR?").strip())
        check_opc()
        check_error()
        diff_current = abs(set_current - float(current))
        req_time = str(diff_current/float(rate))

        psu.write("SOUR:CURR:MODE WAVE")
        check_opc()
        check_error()
        mode = psu.query("SOUR:CURR:MODE?").strip()
        check_opc()
        check_error()    
        if mode == "WAVE":
            psu.write(f"WAVE:CURR {current}")
            check_opc()
            check_error()
            val = float(psu.query("WAVE:CURR?").strip())
            check_opc()
            check_error()
            if val == float(current):
                psu.write(f"WAVE:TIME {req_time}")
                
                val = float(psu.query("WAVE:TIME?").strip())
                check_opc()
                check_error()
                if val == float(req_time):
                    psu.write("INIT")
                    check_opc()
                    check_error()
                    print("INITIALISED")

def start_ramp():
    psu.write("*TRG")
    check_opc()
    check_error()

def set_psu_shutdown():
    set_psu_current(0)
    set_psu_output(0)
    
# -------- thread to listen to commands for PSU ---------------

def execute_command(command):
    try:
        result = eval(command)
        # print(f"Executed: {command}")
        # if result is not None:
        #     print("Result:", result)
    except Exception as e:
        print(f"Error executing command: {str(e)}")

def command_listener():
    while True:
        command = input("Enter command: ").strip()
        if command.lower() == "exit":
            print("Exiting command listener...")
            print("Exited however data logging continuing...")
            break  # Exit the loop and stop the thread
        # Process the command her
        execute_command(command)
        print(command)

# Start the listener in a separate thread
listener_thread = threading.Thread(target=command_listener, daemon=True)
listener_thread.start()


with open(csv_filename, mode="a", newline="") as file:
    file.write(f"# Keithley: {idn.strip()}, PSU: {psu_idn.strip()}, Start: {start_dt}\n")
    writer = csv.writer(file)
    writer.writerow(["Timestamp", "Channel1_V", "PSU_Iout_A", "PSU_Vout_V", "PSU_Vout_MEAS", "AST244_V", 'PT_0', "PT_180"])  # 1 channel read

    # Create the client for InfluxDB

    influx_client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    influx_write_api = influx_client.write_api(write_options=SYNCHRONOUS)

    try:
        while True:
            timestamp = datetime.now().isoformat()
            ch1 = float(read_voltage(1))
            # pt_0 = float(lakeshore224.get_kelvin_reading("A"))
            # pt_180 = float(lakeshore224.get_kelvin_reading("B"))
            pt_0 = 0.0
            pt_180 = 0.0
            task_values = task.read()
            ni9205_ch0 = task_values[0]
            ni9205_ch1 = task_values[1]
            # ch2 = read_voltage(2)
            psu_iout = read_psu_current()
            time.sleep(0.1)
            psu_vout = read_psu_voltage()
            time.sleep(0.1)
            writer.writerow([timestamp, ch1, psu_iout, psu_vout, ni9205_ch1, ni9205_ch0, pt_0, pt_180])  # 1 channel

            # Write to InfluxDB
            # Add new fields simply by: "<name>", value)
            current_time = datetime.utcnow()
            datadict = {"PSU Current": psu_iout,
                        "PSU Voltage": psu_vout,
                        "NanoVolt_Ch1": ch1,
                        "AST244_v": ni9205_ch0,
                        "PSU V_Meas": ni9205_ch1,
                        "PT_000": pt_0,
                        "PT_180": pt_180}

            influx_write_api.write(
                INFLUX_BUCKET_NAME,
                record=[
                    {
                        "measurement": "Coil_Testing",
                        "fields": datadict,
                        "time": current_time,
                    }
                ],
            )

            time.sleep(0.2)

    except KeyboardInterrupt:
        print("Stopped by user.")
    finally:
        keithley.close()
        task.close()
        #lakeshore224.close()
        #psu.close()
