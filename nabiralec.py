#!/usr/bin/env python3

""" 
Program za vodenje robota EV3 do ciljne točke na poligonu.
[Robo liga FRI 2019: Sadovnjak]
"""

__author__ = "Laboratory for adaptive systems and parallel processing"
__copyright__ = "Copyright 2019, UL FRI - LASPP"
__credits__ = ["Laboratory for adaptive systems and parallel processing"]
__license__ = "GPL"
__version__ = "0.1"
__maintainer__ = "Nejc Ilc"
__email__ = "nejc.ilc@fri.uni-lj.si"
__status__ = "Active"


# pip install python-ev3dev
from ev3dev.ev3 import TouchSensor, Button, LargeMotor, Sound
# To install pycurl and ujson on EV3 brick, run:
# sudo apt-get install python3-pycurl
# sudo apt-get install python3-ujson
import pycurl
import ujson
import sys
import math
from io import BytesIO
from time import time, sleep
from enum import Enum
from collections import deque

# ID robota. Spremenite, da ustreza številki, ki je določena vaši ekipi.
ROBOT_ID = 10
# Konfiguracija povezave na strežnik.
# LASPP strežnik ima naslov "193.2.72.3".
SERVER_IP = "193.2.72.100"
# Datoteka na strežniku s podatki o tekmi.
GAME_STATE_FILE = "game.json" 

# Najvišja dovoljena hitrost motorjev.
SPEED_HARD_LIMIT_ABS = 900 
# Najvišja dovoljena nazivna hitrost motorjev pri vožnji naravnost.
# Naj bo manjša kot SPEED_HARD_LIMIT_ABS, da ima robot še možnost zavijati.
SPEED_BASE_MAX = 800

# Parametri za PID
# Obračanje na mestu in zavijanje med vožnjo naravnost
PID_TURN_KP = 3.0
PID_TURN_KI = 0.5
PID_TURN_KD = 0.0
PID_TURN_INT_LIMIT = 100
# Nazivna hitrost pri vožnji naravnost.
PID_STRAIGHT_KP = 2.0
PID_STRAIGHT_KI = 0.5
PID_STRAIGHT_KD = 0.01
PID_STRAIGHT_INT_LIMIT = 100

# Dolzina FIFO vrste za hranjenje zgodovine meritev (oddaljenost in kot do cilja).
HIST_QUEUE_LENGTH = 3

# Razdalje - tolerance
# Dovoljena napaka v oddaljenosti do cilja [mm].
DIST_EPS = 20
# Dovoljena napaka pri obračanju [stopinje].
DIR_EPS = 4
# Bližina cilja [mm]
DIST_CLOSE = 100
# Koliko sekund je robot lahko stanju vožnje naravnost 
# v bližini cilja (oddaljen manj kot DIST_CLOSE),
# preden sprožimo varnostni mehanizem in ga damo 
# v stanje obračanja na mestu.
TIMER_NEAR_TARGET = 3

# Stanja robota
class State(Enum):
    def __str__(self):
        return str(self.name)
    IDLE = 0
    TURN = 1
    DRIVE_STRAIGHT = 2
    LOAD_NEXT_TARGET = 3

class Connection():
    """
    Objekt za vzpostavljanje povezave s strežnikom.
    """
    def __init__(self, url: str):
        """
        Inicializacija nove povezave.
        
        Argumenti:
        url: pot do datoteke na strežniku (URL)
        """
        self._url = url
        self._buffer = BytesIO()
        self._pycurlObj = pycurl.Curl()
        self._pycurlObj.setopt(self._pycurlObj.URL, self._url)
        self._pycurlObj.setopt(self._pycurlObj.CONNECTTIMEOUT, 10)
        self._pycurlObj.setopt(self._pycurlObj.WRITEDATA, self._buffer)
    
    def request(self, debug=False):
        """
        Nalaganje podatkov s strežnika.
        """
        # Počistimo pomnilnik za shranjevanje sporočila
        self._buffer.seek(0,0)
        self._buffer.truncate()
        # Pošljemo zahtevek na strežnik
        self._pycurlObj.perform()
        # Dekodiramo sporočilo
        msg = self._buffer.getvalue().decode()
        # Izluščimo podatke iz JSON
        try:
            return ujson.loads(msg)
        except ValueError as err:
            if debug:
                print('Napaka pri razclenjevanju datoteke JSON: '+ str(err))
                print('Sporocilo streznika:')
                print(msg)
            return -1
    
    def test_delay(self, num_iters: int=10):
        """
        Merjenje zakasnitve pri pridobivanju podatkov o tekmi s strežnika. 
        Zgolj informativno.
        """
        sum_time = 0
        for i in range(num_iters):
            start_time = time()
            if self.request() == -1:
                robot_die()
            elapsed_time = time() - start_time
            sum_time += elapsed_time
        return sum_time / num_iters

class PID():
    """
    Implementacija algoritma za regulacijo PID.
    Nekaj virov za razjasnitev osnovnega načela delovanja:
        - https://en.wikipedia.org/wiki/PID_controller
        - https://blog.opticontrols.com/archives/344
        - https://www.youtube.com/watch?v=d2AWIA6j0NU
    """
    def __init__(self, setpoint: float, Kp: float, Ki: float=None, Kd: float=None, integral_limit:float=None):
        """
        Ustvarimo nov regulator PID s pripadajočimi parametri.

        Argumenti:
        setpoint: ciljna vrednost regulirane spremenljivke
        Kp: ojačitev proporcionalnega dela regulatorja.
            Visoke vrednosti pomenijo hitrejši odziv sistema,
            vendar previsoke vrednosti povzročijo oscilacije in nestabilnost.
        Ki: ojačitev integralnega člena regulatorja.
            Izniči napako v ustaljenem stanju. Zmanjša odzivnost.
        Kd: ojačitev odvoda napake.
            Zmanjša čas umirjanja in poveča odzivnost.
        integral_limit: najvišja vrednost integrala
        """
        self._setpoint = setpoint
        self._Kp = Kp
        self._Ki = Ki
        self._Kd = Kd
        self._integral_limit = integral_limit
        self._error = None
        self._time = None
        self._integral = None
        self._value = None

    def reset(self, setpoint: float=None, Kp: float=None, Ki: float=None, Kd: float=None, integral_limit:float=None):
        """
        Ponastavitev regulatorja. 
        Lahko mu tudi spremenimo katero od vrednosti parametrov ali ciljne vrednosti.
        Napaka, integral napake in čas se ponastavijo.
        """
        if setpoint is not None:
            self._setpoint = setpoint
        if Kp is not None:    
            self._Kp = Kp
        if Ki is not None:
            self._Ki = Ki
        if Kd is not None:
            self._Kd = Kd
        if integral_limit is not None:
            self._integral_limit = integral_limit
        self._error = None
        self._time = None
        self._integral = None
        self._value = None

    def update_control_value(self, measured_value: float) -> float:
        """
        Izračunamo vrednost izhoda regulatorja (regulirna veličina) 
        glede na izmerjeno vrednost regulirane veličine (measured_value) in ciljno vrednost (setpoint).

        Argumenti:
        measured_value: s tipali izmerjena vrednost regulirane veličine

        Izhodna vrednost:
        regulirna veličina, s katero želimo popraviti delovanje sistema (regulirano veličino), da bo dosegel ciljno vrednost
        """
        if self._value is None:
            # Na začetku še nimamo zgodovine meritev, zato inicializiramo integral in vrnemo samo proporcionalni člen.
            self._value = measured_value
            # Zapomnimo si začetni čas.
            self._time = time()
            # Ponastavimo integral napake.
            self._integral = 0
            # Napaka = ciljna vrednost - izmerjena vrednost regulirane veličine.
            self._error = self._setpoint - measured_value
            return self._Kp * self._error
        else:
            # Sprememba časa
            time_now = time()
            delta_time = time_now - self._time
            self._time = time_now
            # Izmerjena vrednost regulirane veličine.
            self._value = measured_value
            # Napaka = ciljna vrednost - izmerjena vrednost regulirane veličine.
            error = self._setpoint - self._value
            
            # Proporcionalni del
            P = self._Kp * error
            
            # Integralni in odvodni člen sta opcijska.
            if self._Ki is None:
                I = 0
            else:
                # Integral se poveča za (sprememba napake) / (sprememba časa)
                self._integral += error * delta_time
                # Ojačitev integralnega dela
                I = self._Ki * self._integral
                if self._integral_limit is not None:
                    I = max(min(I, self._integral_limit), (-1)*(self._integral_limit))
            
            if self._Kd is None:
                D = 0
            else:
                # Odvod napake z ojačitvijo.
                D = self._Kd * (error - self._error) / delta_time
            # Posodobimo napako
            self._error = error
            # Vrnemo regulirno veličino, sestavljeno iz proporcionalnega, integralnega in odvodnega člena.
            return P + I + D

class Point():
    """
    Razred, ki definira točko na poligonu.
    """
    def __init__(self, position):
        self.x = position[0]
        self.y = position[1]
    def __str__(self):
        return '('+str(self.x)+', '+str(self.y)+')'

def get_angle(p1, a1, p2) -> float:
    """
    Izračunaj kot, za katerega se mora zavrteti robot, da bo obrnjen proti točki p2.
    Robot se nahaja v točki p1 in ima smer (kot) a1.
    """
    a = math.degrees(math.atan2(p2.y-p1.y , p2.x - p1.x))
    a_rel = a - a1
    if abs(a_rel) > 180:
        if a_rel > 0:
            a_rel = a_rel - 360
        else:
            a_rel = a_rel + 360
    
    return a_rel

def get_distance(p1:Point, p2:Point) -> float:
    """
    Evklidska razdalja med dvema točkama na poligonu.
    """
    return math.sqrt( (p2.x-p1.x)**2 + (p2.y-p1.y)**2 )

def init_large_motor(port:str) -> LargeMotor:
    """
    Preveri, ali je veliki motor priklopljen na izhod port in vrne objekt za veliki motor.
    """
    motor = LargeMotor(port)
    while not motor.connected:
        print('\nPriklopi motor na izhod '+ port +' in pritisni + spusti gumb DOL.')    
        wait_for_button('down')
        motor = LargeMotor(port)
    return motor

def init_sensor_touch() -> TouchSensor:
    """
    Preveri, ali je tipalo za dotik priklopljeno na katerikoli vhod in vrne objekt za tipalo.
    """
    sensor = TouchSensor()
    while not sensor.connected:
        print('\nPriklopi tipalo za dotik in pritisni + spusti gumb DOL.')    
        wait_for_button('down')
        sensor = TouchSensor()
    return sensor

def wait_for_button(btn_name:str='down'):
    """
    Čakaj v zanki dokler ni gumb pritisnjen in nato sproščen.
    """
    while not getattr(btn, btn_name):
        pass
    flag=False
    while getattr(btn, btn_name):
        if not flag:
            flag=True

def beep(duration=1000,freq=440):
    """
    Potrobi za čas duration in s frekvenco freq. Klic ne blokira.
    """
    Sound.tone(freq, duration)
    # Če želimo, da blokira, dokler se pisk ne konča.
    #Sound.tone(freq, duration).wait()

def robot_die():
    """
    Končaj s programom na robotu. Ustavi motorje.
    """
    print('KONEC')
    motor_left.stop(stop_action='brake')
    motor_right.stop(stop_action='brake')
    Sound.play_song((
        ('D4', 'e'),
        ('C4', 'e'),
        ('A3', 'h')))
    sys.exit(0)



#--------------------------------------------------------------------------
# NASTAVITVE TIPAL, MOTORJEV IN POVEZAVE S STREŽNIKOM
#--------------------------------------------------------------------------
# Nastavimo tipala in gumbe
print('Priprava tipal ... ', end='')
btn = Button()
sensor_touch = init_sensor_touch()
print('OK!')

# Nastavimo velika motorja. Priklopljena naj bosta na izhoda A in D.
print('Priprava motorjev ... ', end='')
motor_left = init_large_motor('outA')
motor_right = init_large_motor('outD')
print('OK!')

# Nastavimo povezavo s strežnikom
url = SERVER_IP+'/'+GAME_STATE_FILE
print('Vspostavljanje povezave z naslovom ' + url + ' ... ', end='')
conn = Connection(url)
print('OK!')

# Izmerimo zakasnitev pri pridobivanju podatkov (povprečje num_iters meritev)
print('Izracun zakasnitve v komunikaciji s streznikom ... ', end='', flush=True)
print('%.4f s' %(conn.test_delay(num_iters=10)))


#--------------------------------------------------------------------------
# PRIPRAVA NA TEKMO
#--------------------------------------------------------------------------
# Pridobimo podatke o tekmi.
game_state = conn.request()
# Ali naš robot sploh tekmuje? Če tekmuje, ali je team1 ali team2?
if ROBOT_ID == game_state['team1']['id']:
    team_my_tag = 'team1'
    team_op_tag = 'team2'
elif ROBOT_ID == game_state['team2']['id']:
    team_my_tag = 'team2'
    team_op_tag = 'team1'
else:
    print('Robot ne tekmuje.')
    robot_die()
print('Robot tekmuje in ima interno oznako "' + team_my_tag +'"')

# Doloci cilj za robota (seznam točk na poligonu). 
# Našem primeru se bo vozil po notranjih kotih obeh košar.
targets_list = [
    Point(game_state['field']['baskets'][team_my_tag]['bottomRight']),
    Point(game_state['field']['baskets'][team_my_tag]['topRight']),
    Point(game_state['field']['baskets'][team_op_tag]['topLeft']),
    Point(game_state['field']['baskets'][team_op_tag]['bottomLeft']),
]
print('Cilji:')
for trgt in targets_list:
    print('\t' + str(trgt))


#--------------------------------------------------------------------------
# GLAVNA ZANKA
#--------------------------------------------------------------------------
print('Izvajam glavno zanko. Prekini jo s pritiskon na tipko DOL.')

# Začetno stanje
state = State.IDLE
# Prejšnje stanje
state_old = -1
# Indeks trenutne ciljne lokacije
target_idx = 0

# Regulator PID za obračanje na mestu.
# setpoint=0 pomeni, da mora biti kot med robotom in ciljem (target_angle) enak 0.
# Naša regulirana veličina je torej kar napaka kota, ki mora biti 0. To velja tudi za 
# regulacijo vožnje naravnost.
PID_turn = PID(setpoint=0, Kp=PID_TURN_KP, Ki=PID_TURN_KI, Kd=PID_TURN_KD, integral_limit=PID_TURN_INT_LIMIT)
# PID za vožnjo naravnost - regulira nazivno hitrost za oba motorja, ki je odvisna od oddaljenosti od cilja.
# setpoint=0 pomeni, da mora biti razdalja med robotom in ciljem enaka 0.
PID_forward_base = PID(setpoint=0, Kp=PID_STRAIGHT_KP,Ki=PID_STRAIGHT_KI,Kd=PID_STRAIGHT_KD, integral_limit=PID_STRAIGHT_INT_LIMIT)
# PID za obračanje med vožnjo naravnost.
# setpoint=0 pomeni, da mora biti kot med robotom in ciljem (target_angle) enak 0.
PID_forward_turn = PID(setpoint=0, Kp=PID_TURN_KP, Ki=PID_TURN_KI, Kd=PID_TURN_KD, integral_limit=PID_TURN_INT_LIMIT)

# Hitrosti na obeh motorjih
speed_right = 0
speed_left = 0

# Zgodovina (okno) zadnjih nekaj vrednosti meritev, implementirana kot vrsta FIFO.
robot_dir_hist = deque([180.0] * HIST_QUEUE_LENGTH)
robot_dist_hist = deque([math.inf] * HIST_QUEUE_LENGTH)

# Merimo čas obhoda zanke
t_old = time()

do_main_loop = True  
while do_main_loop and not btn.down:

    time_now = time()
    loop_time = time_now - t_old
    t_old = time_now
    #print(loop_time)

    # Zaznaj spremembo stanja
    if state != state_old:
        state_changed = True
        #print(str(state_old)+"\t->\t" + str(state))
    else:
        state_changed = False
    state_old = state

    # Trenutni cilj
    target = targets_list[target_idx]

    # Osveži stanje tekme
    game_state = conn.request()
    if game_state == -1:
        print('Napaka v paketu, ponovni poskus ...')
    else:
        game_on = game_state['gameOn']
        time_left = game_state['timeLeft']
        
        # Pridobi pozicijo in orientacijo svojega robota;
        # najprej pa ga poišči v tabeli vseh robotov na poligonu.
        robot_pos = None
        robot_dir = None
        for robot_data in game_state['robots']:
            if robot_data['id'] == ROBOT_ID:
                robot_pos = Point(robot_data['position'][0:2])
                robot_dir = robot_data['direction']
        # Ali so podatki o robotu veljavni? Če niso, je zelo verjetno, da sistem ne zazna oznake na robotu.
        robot_alive = (robot_pos is not None) and (robot_dir is not None)

        # Če tekma poteka in je oznaka robota vidna na kameri, potem izračunamo novo hitrost na motorjih.
        # Sicer motorje ustavimo.
        if game_on and robot_alive:
            # Razdalja med robotom in ciljem
            target_dist = get_distance(robot_pos, target)
            # Kot med robotom in ciljem
            target_angle = get_angle(robot_pos, robot_dir, target)  

            # Spremljaj zgodovino meritev kota in oddaljenosti.
            # Odstrani najstarejši element in dodaj novega - princip FIFO.
            robot_dir_hist.popleft()
            robot_dir_hist.append(target_angle)            
            robot_dist_hist.popleft()
            robot_dist_hist.append(target_dist)

            if state == State.IDLE:
                # Stanje mirovanja - tu se odločamo, kaj bo robot počel v nadaljevanju.
                speed_right = 0
                speed_left = 0
                # Preverimo, ali je robot na cilju. Če ni, ga tja pošljemo.                             
                if target_dist > DIST_EPS:
                    state = State.TURN
                    robot_near_target_old = False             
                else:
                    state = State.LOAD_NEXT_TARGET
            
            elif state == State.LOAD_NEXT_TARGET:
                # Naložimo naslednjo ciljno točko iz seznama
                target_idx = target_idx + 1
                # Če smo prišli do konca seznama, gremo spet od začetka
                if target_idx >= len(targets_list):
                    target_idx = 0
                state = State.IDLE

            elif state == State.TURN:
                # Obračanje robota na mestu, da bo obrnjen proti cilju
                if state_changed:
                    # Če smo ravno prišli v to stanje, najprej ponastavimo PID
                    PID_turn.reset()
                
                # Ali smo že dosegli ciljni kot? 
                # Zadnjih nekaj obhodov zanke mora biti absolutna vrednost napake kota manjša od DIR_EPS.
                err = [abs(a) > DIR_EPS for a in robot_dir_hist]
                
                if sum(err) == 0:
                    # Vse vrednosti so znotraj tolerance, zamenjamo stanje.
                    speed_right = 0
                    speed_left = 0
                    state = State.DRIVE_STRAIGHT
                else:
                    # Reguliramo obračanje. Ker se v regulatorju trenutna napaka izračuna kot: 
                    #   error = setpoint - measured_value,
                    # dobimo negativno vrednost, ko se moramo zavrteti v pozitivno smer.
                    # Primer: 
                    #   Robot ima smer 90 stopinj (obrnjen je proti "severu").
                    #   Cilj se nahaja na njegovi levi in da ga doseže, se mora obrniti za 90 stopinj.
                    #   setpoint=0
                    #   target_angle = measured_value = 90
                    #   error = setpoint - measured_value = -90
                    #   u = funkcija, odvisna od error in parametrov PID. Če imamo denimo Kp = 1, Ki = Kd = 0, potem bo u = -90.
                    #   Robot se mora zavrteti v pozitivno smer, torej z desnim kolesom naprej in levim nazaj.
                    #   Zato:
                    #   speed_right = -u
                    #   speed_left = u
                    #   Lahko bi tudi naredili droben trik in bi rekli: measured_value= -target_angle.
                    #   V tem primeru bi bolj intuitivno nastavili speed_right = u in speed_left = -u.
                    u = PID_turn.update_control_value(measured_value=target_angle)
                    speed_right = -u
                    speed_left = u
            
            elif state == State.DRIVE_STRAIGHT:
                # Vožnja robota naravnost proti ciljni točki. 
                # Vmes bi radi tudi zavijali, zato uporabimo dva regulatorja.
                if state_changed:
                    # Ponastavi regulatorja PID
                    PID_forward_base.reset()                 
                    PID_forward_turn.reset()
                    timer_near_target = TIMER_NEAR_TARGET

                # Ali smo blizu cilja?
                robot_near_target = target_dist < DIST_CLOSE
                if not robot_near_target_old and robot_near_target:
                    # Vstopili smo v bližino cilja.
                    # Začnimo odštevati varnostno budilko.
                    timer_near_target = TIMER_NEAR_TARGET
                if robot_near_target:
                    timer_near_target = timer_near_target - loop_time
                    print(timer_near_target)
                robot_near_target_old = robot_near_target

                # Ali smo že na cilju? 
                # Zadnjih nekaj obhodov zanke mora biti razdalja do cilja manjša ali enaka DIST_EPS.
                err_eps = [d > DIST_EPS for d in robot_dist_hist]                
                if sum(err_eps) == 0:
                    # Razdalja do cilja je znotraj tolerance, zamenjamo stanje.
                    speed_right = 0
                    speed_left = 0
                    state = State.LOAD_NEXT_TARGET
                elif timer_near_target < 0:
                    # Ali smo morda blizu cilja, vendar nikakor ne pridemo do njega?
                    speed_right = 0
                    speed_left = 0
                    state = State.TURN
                else:
                    u_turn = PID_forward_turn.update_control_value(measured_value=target_angle)
                    # Ker je napaka izračunana kot setpoint - measured_value in smo nastavili setpoint na 0,
                    # bomo v primeru u_base dobili negativne vrednosti takrat, ko se bo robot moral premikati naprej.
                    # Zato dodamo minus pri izračunu hitrosti motorjev.
                    u_base = PID_forward_base.update_control_value(measured_value=target_dist)
                    # Omejimo nazivno hitrost, ki je enaka za obe kolesi, da imamo še manevrski prostor za zavijanje.
                    u_base = min(max(u_base, -SPEED_BASE_MAX), SPEED_BASE_MAX)
                    speed_right = -u_base - u_turn
                    speed_left = -u_base + u_turn
                    
            # Omejimo vrednosti za hitrosti na motorjih.
            speed_right = round(min(max(speed_right, -SPEED_HARD_LIMIT_ABS), SPEED_HARD_LIMIT_ABS))
            speed_left = round(min(max(speed_left, -SPEED_HARD_LIMIT_ABS), SPEED_HARD_LIMIT_ABS))

            # Vrtimo motorje
            motor_right.run_forever(speed_sp=speed_right)
            motor_left.run_forever(speed_sp=speed_left)

        else:
            # Robot bodisi ni viden na kameri bodisi tema ne teče.
            motor_left.stop(stop_action='brake')
            motor_right.stop(stop_action='brake')

# Konec programa
robot_die()

    
