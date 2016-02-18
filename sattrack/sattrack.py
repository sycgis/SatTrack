__author__ = 'Ibrahim'

import ephem
from datetime import datetime
import requests
import time
import threading as th
import serial
from interface import *
import webbrowser as wb
import struct
from .helpers import *

MOTOR_DEBUG_MODE = False

class SatTrack:
    def __init__(self):
        self.id = None
        self.observer = ephem.Observer()
        self.satellite = None
        self.threads = {}
        self.lock = th.Lock()
        self.interval = 1
        self._isActive = False
        self.azmotor = None
        self.altmotor = None
        self.server = Server()
        self.tle = None
        self.stopTracking = th.Event()
        self.stopComputing = th.Event()
        
        if MOTOR_DEBUG_MODE:
            print "Debug Mode..."

    def set_location(self, lat='36.1486', lon='-86.8050', ele=182):
        """
        Sets location of observer using coordinates (+N and +E). Defaults to Vanderbilt University's coordinates.
        :param lat: Observer's latitude (positive north from the equator)
        :param lon: Observer's longitide (positive east from the prime meridian)
        :param ele: Observer's elevation above sea level.
        """
        self.observer.lat = lat
        self.observer.lon = lon
        self.observer.elev = ele
        self.observer.epoch = ephem.Date(str(datetime.utcnow()))
        self.observer.date = ephem.Date(str(datetime.utcnow()))
        
    def load_tle(self, filename):
        """
        loads satellite TLE data from a text file
        :param filename: path of file
        """
        f = open(filename, 'rb')
        data = f.readlines()
        f.close()
        self.satellite = ephem.readtle(data[0], data[1], data[2])
        self.tle = data
        self.id = sanitize(data[0])
    
    def get_tle(self, noradid, destination=None):
        """
        parses CELESTRAK and AMSAT for satellite's TLE data using its NORAD id.
        :param noradid: Satellite's designation
        :param destination: Place to save the data for later use (optional).
        """
        data = parse_text_tle(noradid, AMSAT_URL)
        if not data:
            data = parse_text_tle(noradid, base_CELESTRAK_URL, CELESTRAK_paths)
        if destination is not None:             # write to destination if provided
            f = open(destination, 'wb')
            wdata = [str(noradid) + '\n'] + [data[0] + '\n'] + [data[1] + '\n']
            f.writelines(wdata)
            f.close()
        self.satellite = ephem.readtle(noradid, data[0], data[1])
        self.tle = [str(noradid), data[0], data[1]]
        self.id = sanitize(str(noradid))
    
    def begin_computing(self, interval=1.0, trace=0.0, display=False):
        """
        Starts a thread that computes satellite's coordinates in real time based on the observer's coordinates.
        :param interval: Time between computations.
        :param display: To show a map with the satellite's location.
        """
        self.interval = interval
        t = th.Thread(target=self._update_coords, args=[interval, trace])
        t.daemon = True
        self.threads['tracker'] = t
        t.start()

    def _update_coords(self, t, trace):
        """
        This function runs in a thread started in 'begin_computing' and recomputes the satellites position after
        interval 't'.
        :param t: Interval between computations
        """
        while not self.stopComputing.isSet():
            with self.lock:
                try:
                    if trace > 0:
                        self.observer.date = ephem.Date(self.observer.date + trace * ephem.second)
                    else:
                        self.observer.date = ephem.Date(str(datetime.utcnow()))
                    self.satellite.compute(self.observer)
                    self._isActive = True
                except:
                    self._isActive = False
            time.sleep(t)
    
    def visualize(self, host='localhost', openbrowser=True):
        """
        Start a local server and visualize satellite position. URL is of the format: http://localhost:8000/<name>/
        <name> is sanitized by removing any non-alphanumeric characters.
        :param openbrowser: False -> start server only, True -> start server and open browser.
        """
        self.server.add_source(self)
        if not Server.server:
            self.server.start_server(host, new=True)
        if openbrowser:
            url = 'http://' + str(self.server.host) + ':' + str(self.server.port) + '/' + self.id + '/'
            url = sanitize_url(url)
            print "opening URL: " + url
            wb.open(url, new=2)
    
    def begin_tracking(self, interval=None):
        """
        Begins a thread that sends periodic commands to servo motors.
        :param interval: Time between commands in seconds.
        :return:
        """
        if interval is None:
            interval = self.interval
        t = th.Thread(target=self._update_tracker, args=[interval])
        t.daemon = True
        self.threads['motors'] = t
        t.start()

    def _update_tracker(self, t):
        """
        This function runs in a thread started by 'begin_tracking' and moves altitude and azimuth servo motors every
        't' seconds if the change in coordinates is greater than the motors' resolutions.
        :param t: Interval between commands to motors.
        """
        while not self.stopTracking.isSet():
            with self.lock:
                az = to_degs(self.satellite.az)
                alt = to_degs(self.satellite.alt)
            if self._isActive:
                if abs(az - self.azmotor.current_pos) >= self.azmotor.resolution:
                    self.azmotor.move(int(az))
                if abs(alt - self.altmotor.current_pos) >= self.altmotor.resolution:
                    self.altmotor.move(int(alt))
            time.sleep(t)
    
    def is_trackable(self):
        """
        Checks if the satellite's current position can be tracked by the motors (accounting for range and orientation)
        """
        with self.lock:
            vertically = (to_degs(self.satellite.alt) >= self.altmotor.range[0] + self.altmotor.pos0) and (to_degs(self.satellite.alt) <= self.altmotor.range[1] + self.altmotor.pos0)
            horizontally = (to_degs(self.satellite.az) >= self.azmotor.range[0] + self.azmotor.pos0) and (to_degs(self.satellite.az) <= self.azmotor.range[1] + self.azmotor.pos0)
            return vertically and horizontally

    def is_observable(self):
        """
        Checks if the satellite can be seen above the horizon
        """
        with self.lock:
            return self.satellite.alt >= self.observer.horizon

    def connect_servos(self, port=2, motors=(1,2), minrange=(0, 0), maxrange=(180, 360), initpos=(0, 0)):
        """
        Connects computer to arduino which has a pair of servos connected. Initializes motors to their default positions
        :param port: port name/number e.g 'COM3' on a PC, '/dev/ttyUSB0' on Linux, '/dev/tty.usbserial-FTALLOK2' on Mac
        :param minrange: A touple containing the minimum angle for (altitude, azimuth) motors
        :param maxrange: A touple containing the maximum angle for (altitude, azimuth) motors
        :param initpos: A touple containing the initial orientation angle for (altitude, azimuth) motors
        """
        servos = ServoController(port, motors)
        self.altmotor, self.azmotor = servos.motors
        self.altmotor.range = (minrange[0], maxrange[0])
        self.azmotor.range = (minrange[1], maxrange[1])
        self.altmotor.pos0 = initpos[0]
        self.azmotor.pos0 = initpos[1]
        self.azmotor.initialize()
        self.altmotor.initialize()


    def next_pass(self, datestr=None, convert=True):
        """
        Takes date as string (or defaults to current time) to compute the next closest pass of a satellite for the
        observer. Times are returned in local time zone, and angles are returned in degrees.
        :param datestr: string containing date
        :param convert: whether or not to convert to time string/ angle degrees
        :return: a dictionary of values
        """
        if datestr is not None:
            date = parser.parse(datestr)
            date = date.replace(tzinfo=tz.tzlocal())
            dateutc = ephem.Date(str(date.astimezone(tz.tzutc())))
        else:
            dateutc = ephem.Date(str(datetime.utcnow()))
        observer = ephem.Observer()             # create a copy of observer
        observer.lon = self.observer.lon
        observer.lat = self.observer.lat
        observer.elev = self.observer.elev
        observer.epoch = self.observer.epoch
        observer.date = ephem.Date(dateutc + ephem.minute)
        satellite = ephem.readtle(self.tle[0], self.tle[1], self.tle[2])        # make a copy of satellite
        satellite.compute(observer)
        next_pass = observer.next_pass(satellite)
        if convert:
            next_pass = {'risetime': tolocal(next_pass[0]), 'riseaz': to_degs(next_pass[1]), 'maxtime': tolocal(next_pass[2])
                        , 'maxalt': to_degs(next_pass[3]), 'settime': tolocal(next_pass[4]), 'setaz': to_degs(next_pass[5])}
        else:
            next_pass = {'risetime': next_pass[0], 'riseaz': next_pass[1], 'maxtime': next_pass[2]
                        , 'maxalt': next_pass[3], 'settime': next_pass[4], 'setaz': next_pass[5]}
        return next_pass

    def next_passes(self, n, startdate=None, convert=True):
        """
        calculate the next n passes starting from the given date (or default to current time). Dates calculated are
        in local time.
        :param n: number of passes to calculate
        :param startdate: earliest time to calculate from
        :param convert: convert to degree angles and string dates?
        :return: a list of dictionaries for each pass
        """
        result = []
        for i in range(n):
            result.append(self.next_pass(startdate, convert))
            startdate = str(result[i]['settime'])
        return result

    def show_position(self):
        """
        prints satellite's coordinates and relative position periodically.
        """
        i = 0
        while self._isActive:
            i += 1
            with self.lock:
                print('#' + str(i) + ', Azimuth: ' + str(self.satellite.az) + ', Altitude: ' + str(self.satellite.alt) +
                      ' Lat: ' + str(self.satellite.sublat) + ' Lon: ' + str(self.satellite.sublong) + '\r'),
            time.sleep(self.interval)
    
    def curr_pos(self):
        with self.lock:
            return {'alt': to_degs(self.satellite.alt), 'az': to_degs(self.satellite.az), 'lat': to_degs(self.satellite.sublat), 'lon': to_degs(self.satellite.sublong)}
            

    def write_to_file(self, fname, secs):
        i = 0
        f = open(fname, 'wb')
        while i < secs:
            i += 1
            with self.lock:
                f.write('#' + str(i) + ' Azimuth: ' + str(self.satellite.az) + ' Altitude: ' + str(self.satellite.alt) + '\n')
            time.sleep(self.interval)
        f.close()
    
    def stop(self):
        self.stopComputing.set()
        self.stopTracking.set()
        try:
            self.threads['tracker'].join(timeout=self.interval)
            self.threads['motors'].join(timeout=self.interval)
            self.stopComputing.clear()
            self.stopTracking.clear()
            self._isActive = False
        except LookupError as e:
            pass
        print "stopped"
            
            
class ServoController:
    def __init__(self, port=2, motors=(1,2), baudrade=9600, timeout=1):
        self.port = port
        self.baudrate = baudrade
        self.timeout = timeout
        try:
            self.serial = serial.Serial(port, baudrade, timeout=timeout)
        except serial.SerialException as e:
            print e.message
        self.motors = [Motor(i, self.serial) for i in motors]


class Motor:
    def __init__(self, id, port):
        self.motor = id
        self.resolution = 1
        self.range = (0, 180)
        self.current_pos = 0
        self.pos0 = 0
        self.port = port
        self.map = lambda x: x

    def initialize(self):
        midpoint = (self.range[0] + self.range[1]) / 2
        self.move(midpoint)

    def move(self, angle):
        mapped_angle = self.map(angle) - self.pos0
        if MOTOR_DEBUG_MODE:
            mapped_angle = abs(mapped_angle)
        if (angle < self.range[0] or angle > self.range[1]) and not MOTOR_DEBUG_MODE:
            raise ValueError('Motor ' + str(self.motor) + ' angle out of range:' + str(angle))
        serial_arg = 's' + str(self.motor) + 'a' + str(mapped_angle)
        self.port.write(serial_arg)
        if MOTOR_DEBUG_MODE:
            print mapped_angle
            #print self.port.read(10).strip()
        self.current_pos = angle


def store_passes(satellite, outpath, n=100):
    data = satellite.next_passes(n)
    import csv
    keys = ['risetime', 'riseaz', 'maxtime', 'maxalt', 'settime', 'setaz']
    with open(outpath, 'wb') as output_file:
        dict_writer = csv.DictWriter(output_file, keys)
        dict_writer.writeheader()
        dict_writer.writerows(data)


def load_config(filepath):
    settings = read_settings(f)


if __name__ == '__main__':
   pass

