#!/usr/bin/env python3

import  os
import  sys
import  argparse
import  serial
import  subprocess
import  random
import  threading
import  asyncio
import  queue
import  traceback
import  tempfile

from time import sleep
from datetime import datetime

from bokeh.server.server import Server
from bokeh.application import Application
from bokeh.application.handlers.function import FunctionHandler
from bokeh.plotting import figure, ColumnDataSource
from bokeh.models import Range1d
from bokeh.layouts import gridplot

from    open_source_libs.p3lib.uio import UIO
from    open_source_libs.p3lib.helper import logTraceBack, appendCreateFile

class Reading(object):
    """@brief Resonsible for holding a reading value."""
    def __init__(self, value, timeStamp=None):
        """@brief Constructor
           @param value The Y value
           @param timeStamp The x Value."""
        if timeStamp:
            self.time = timeStamp
        else:
            self.time = datetime.now()
        self.value = value
    
class Plotter(object):
    """@brief Responsible for plotting the DMM values."""

    def __init__(self, label, yRangeLimits=[], bokehPort=5001):
        """@brief Constructor.
           @param label The label associated with the trace to plot.
           @param yRangeLimits Limits of the Y axis. By default auto range.
           @param bokehPort The TCP IP port for the bokeh server."""
        self._label=label
        self._yRangeLimits=yRangeLimits
        self._bokehPort=bokehPort
        self._source = ColumnDataSource({'x': [], 'y': [], 'color': []})
        self._evtLoop = None
        self._queue = queue.Queue()
        
    def runBokehServer(self):
        """@brief Run the bokeh server. This is a blocking method."""
        apps = {'/': Application(FunctionHandler(self._createPlot))}
        #As this gets run in a thread we need to start an event loop
        evtLoop = asyncio.new_event_loop()
        asyncio.set_event_loop(evtLoop)
        server = Server(apps, port=self._bokehPort)
        server.start()
        #Show the server in a web browser window
        server.io_loop.add_callback(server.show, "/")
        server.io_loop.start()
        
    def _createPlot(self, doc, ):
        """@brief create a plot figure.
           @param doc The document to add the plot to."""
        if self._yRangeLimits and len(self._yRangeLimits) == 2:
            yrange = Range1d(self._yRangeLimits[0],self._yRangeLimits[1])
        else:
            yrange = None
        fig = figure(title='MS8236 DMM', 
                     toolbar_location='above', 
                     x_axis_type="datetime",
                     x_axis_location="below",
                     y_range=yrange)
        fig.yaxis.axis_label = self._label
        fig.line(source=self._source)
        grid = gridplot(children = [[fig]], sizing_mode = 'stretch_both')
        doc.title = self._label
        doc.add_root(grid)
        doc.add_periodic_callback(self._update, 100)
              
    def _update(self):
        """@brief called periodically to update the plot trace."""
        while not self._queue.empty():
            reading = self._queue.get()                       
            new = {'x': [reading.time],
                   'y': [reading.value],
                   'color': ['blue']}
            self._source.stream(new)

    def addValue(self, value, timeStamp=None):
        """@brief Add a value to be plotted
           @param value The Y value to be plotted."""
        reading = Reading(value, timeStamp=timeStamp)
        self._queue.put(reading)
    
class HYLEC_MS8236(object):
    """@brief Responsible for logging data from the HYLEC MS8236 DMM"""
        
    DEFAULT_SERIAL_PORT = "/dev/ttyUSB0"
    LOG_FILENAME        = "dmm.log"
    DEFAULT_LOG_FILE    = os.path.join( tempfile.gettempdir(), LOG_FILENAME)
    DIGIT_VALUE_LIST    = [ 0x00,0x5f,0x06,0x6b,0x2f,0x36,0x3d,0x7d,0x07,0x7f,0x3f,0x58 ]
    DIGIT_STR_LIST      = [ "" , "0", "1", "2", "3", "4", "5", "6", "7", "8", "9","L" ]
    MSG_ID_0            = 0xaa
    MSG_ID_1            = 0x55
    VALID_MESSAGE_LEN   = 22
        
    def __init__(self, uio, options):
        """@brief Constructor
           @param uio A UIO instance handling user input and output (E.G stdin/stdout or a GUI)
           @param options An instance of the OptionParser command line options."""
        self._uio = uio
        self._options = options
        
        self._serial = None       
        self._plotter = None

    def _openSerialPort(self):
        """@brief Open the serial port with the required parameters"""
        self._uio.info("Open serial port: {}".format(self._options.plot))
        self._serial = serial.Serial(
            port=self._options.port,
            baudrate=2400,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            bytesize=serial.EIGHTBITS
        )
        self._uio.info("Opened serial port")
        
    def _getDigitStr(self, value):
        """@brief Get a digit string from a digit value.
           @param value The digit value.
           @return A String representing the displayed digit or None if no match found."""
        valueStr = None
        addDecPoint=False
        if value&0x80:
            addDecPoint=True
        value=value&0x7f
        for index in range(0,12):
            if HYLEC_MS8236.DIGIT_VALUE_LIST[index] == value:
                valueStr = HYLEC_MS8236.DIGIT_STR_LIST[index]
        if addDecPoint:
            valueStr="."+valueStr
        return valueStr

    def _getText(self, value, mapList):
        """@brief Get a String associated with the bits set in the byte value.
           @param value The value that in which set bits define Strings.
           @param mapList A list of strings that to map to each set bit.
           @return A string from the mapList"""
        strList = []
        for index in range(0,8):
            if value&1:
                strList.append(mapList[index])
            value=value >> 1
        return "".join(strList) 
    
    def _isValidFrame(self, rxValueList):
        """@brief Determine if we have a valid RX data frame.
           @param rxValueList
           @return True if a valid frame of data."""
        valid=False
        if len(rxValueList) == HYLEC_MS8236.VALID_MESSAGE_LEN and\
           rxValueList[0] == HYLEC_MS8236.MSG_ID_0 and\
           rxValueList[1] == HYLEC_MS8236.MSG_ID_1:
            valid = True
        return valid
        
    def _recordLog(self, value, label):
        """@brief Record data to the log file.
           @param value The value to be saved.
           @param label The label associated with the value that defines the measurement value."""
        timeStr = datetime.now().strftime("%d/%m/%Y-%H:%M:%S.%f")
        self._uio.info("{}: {} {}".format(timeStr, value, label))
        fd = open(self._options.log, 'a')
        fd.write("{}: {} {}\n".format(timeStr, value, label))
        fd.close()
     
    def _getYRange(self):
        """@brief Get the Y range.
           @return a tuple with min,max or None if not defined (autorange)"""
        yRange=None
        if self._options.range:
            elems = self._options.range.split(",")
            if len(elems) == 2:
                try:
                    min=int(elems[0])
                    max=int(elems[1])
                except ValueError:
                    pass
                yRange = (min, max)
        return yRange
             
    def _sendPlotValue(self, value, label):
        """@brief Send a value to be plotted."""
        if self._options.plot:
            if not self._plotter:
                self._plotter = Plotter(label, self._getYRange())
                bt = threading.Thread(target=self._plotter.runBokehServer)
                bt.setDaemon(True)
                bt.start()

            self._plotter.addValue(value)
                
    def _processRXData(self, rxValueList):
        """@brief Process a data from from the meter.
           @param rxValueList A list of values received from the meter.""" 
        if self._isValidFrame(rxValueList):    
            d1 = self._getDigitStr(rxValueList[9])
            d2 = self._getDigitStr(rxValueList[8])                
            d3 = self._getDigitStr(rxValueList[7])
            d4 = self._getDigitStr(rxValueList[6])
            try:
                value = float(d1+d2+d3+d4)        
            except ValueError:
                value = d1+d2+d3+d4               

            text1 = self._getText(rxValueList[20], ["DegC ","DegF ","?","?","m","u","n","F "])

            text2 = self._getText(rxValueList[21], ["u","m","A ","V ","M","k","Ohms ","Hz "])
                
            text3 = self._getText(rxValueList[10]&0xE7, ["Diode ","AC ","DC ","-","-","","Continuity ","LowBattery "])
                
            text4 = self._getText(rxValueList[18], ["","","","","Wait ","Auto ","Hold ","REL "])
                            
            text5 = self._getText(rxValueList[19], ["","MAX","-","MIN","N/A","%","hFE","N/A"])

            label = "{}{}{}{}{}".format(text1, text2, text3, text4, text5)
            
            self._recordLog(value, label)
            
            #If the value is float we could plot it if required
            if isinstance(value, float): 
                self._sendPlotValue(value, label)           
                     
    def _loadLog(self):
        """@brief Load from log file
           @return A list of elements
                   0: xValueList
                   1: yValueList
                   2: label"""
        xValueList=[]
        yValueList=[]
        fd = open(self._options.log,'r')
        lines = fd.readlines()
        fd.close()
        for line in lines:
            elems = line.split()
            if len(elems) > 2:
                tStr = elems[0]
                if tStr.endswith(":"):
                    tStr=tStr[:-1]
                try:
                    yValue = float(elems[1])
                    xValue = datetime.strptime(tStr, "%d/%m/%Y-%H:%M:%S.%f")
    
                    xValueList.append(xValue)
                    yValueList.append(yValue)
                    pos = line.find(elems[1])+len(elems[1])
                    label = line[pos:]
                except ValueError:
                    pass

        return (xValueList, yValueList, label)
        
    def _plotFromLog(self):
        """@brief Plot data from the log file."""
        if not os.path.isfile(self._options.log):
            raise Exception("{} file not found".format(self._options.log))
        xValueList, yValueList, label = self._loadLog()
        if len(xValueList) != len(yValueList):
            raise Exception("X/Y Len eror {}/{}".format(len(xValueList),len(yValueList)))
        self._plotter = Plotter(label, self._getYRange())
        bt = threading.Thread(target=self._plotter.runBokehServer)
        bt.setDaemon(True)
        bt.start()
        for index in range(0,len(xValueList)):
            self._plotter.addValue(yValueList[index], xValueList[index])
            index=index+1
        bt.join()
        
    def log(self):
        """@brief Log data from the DMM. """
        if self._options.fplot:
            self._plotFromLog()
            return
        
        appendCreateFile(self._uio, self._options.log)
        self._uio.info("LOG: {}".format(self._options.log))
        try:
            rxValueList = []
            self._openSerialPort()
            while True:
                val = int.from_bytes( self._serial.read(1) , "big")

                if val == HYLEC_MS8236.MSG_ID_0:
                    rxValueList = []
                                 
                rxValueList.append(val)
                
                if len(rxValueList) >= 80:
                    rxValueList.pop()
                    
                if len(rxValueList) == 22:
                    self._processRXData(rxValueList)

        finally:
            
            if self._serial:
                self._serial.close()
                
            print()
            self._uio.info("LOG: {}".format(self._options.log))
                
def main():
    """@brief Program entry point"""
    uio = UIO()

    try:
        parser = argparse.ArgumentParser(description="Log data from the HYLEC MS8236 DMM.\n"\
                                                     "This DMM has a USB interface over which data can be sent.\n"\
                                                     "The program allows you to record and plot this data on a connected PC.",
                                         formatter_class=argparse.RawDescriptionHelpFormatter)
        parser.add_argument("-d", "--debug",  action='store_true', help="Enable debugging.")
        parser.add_argument("-p", "--port",   help="Serial port (default={}).".format(HYLEC_MS8236.DEFAULT_SERIAL_PORT), default=HYLEC_MS8236.DEFAULT_SERIAL_PORT)
        parser.add_argument("-l", "--log",    help="Log file (default={}).".format(HYLEC_MS8236.DEFAULT_LOG_FILE), default=HYLEC_MS8236.DEFAULT_LOG_FILE)        
        parser.add_argument("-t", "--plot",   help="Plot data in real time.", action='store_true')
        parser.add_argument("-f", "--fplot",  help="Plot data from log file.", action='store_true')
        parser.add_argument("-r", "--range",  help="The Y axis range. By default the Y axis will auto range. If defined then a comma separated list of min,max values is required. (E.G 0,10)", default=None)        

        
        parser.epilog = "Example\n"\
                        "dmm -p /dev/ttyUSB1 --log /tmp/dmm.log --plot\n"

        options = parser.parse_args()

        uio.enableDebug(options.debug)
        hylecMS8236 = HYLEC_MS8236(uio, options)
        hylecMS8236.log()

    #If the program throws a system exit exception
    except SystemExit:
        pass
    #Don't print error information if CTRL C pressed
    except KeyboardInterrupt:
        pass
    except Exception as ex:
        logTraceBack(uio)

        if options.debug:
            raise
        else:
            uio.error(str(ex))

if __name__== '__main__':
    main()