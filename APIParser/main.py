import ConfigParser
import psycopg2
import psycopg2.extras
from hotqueue import HotQueue
import gevent
from gevent.pool import Pool
from gevent import monkey; gevent.monkey.patch_all()
import pylibmc
import logging
import eveapi
import urllib
import xml.etree.ElementTree as ET
import httplib

config = ConfigParser.ConfigParser()
config.read(['api.conf', 'local_api.conf'])
dbhost = config.get('Database', 'dbhost')
dbname = config.get('Database', 'dbname')
dbuser = config.get('Database', 'dbuser')
dbpass = config.get('Database', 'dbpass')
dbport = config.get('Database', 'dbport')
redisdb = config.get('Redis', 'redishost')
apiServer = config.get('API', 'host')
mcserver = config.get('Memcache', 'server')
mckey = config.get('Memcache', 'key')
psource = config.get('Pricing', 'source')
ecapi = config.get('Pricing', 'echost')
e43api = config.get('Pricing', 'e43host')
psqlhost = config.get('Pricing', 'psqlhost')
psqlname = config.get('Pricing', 'psqlname')
psqluser = config.get('Pricing', 'psqluser')
psqlpass = config.get('Pricing', 'psqlpass')
psqlport = config.get('Pricing', 'psqlport')

MAX_NUM_POOL_WORKERS = 75

queue = HotQueue("killboard-API", host=redisdb, port=6379, db=0)

logging.basicConfig(format='%(asctime)s:%(levelname)s:%(message)s', level=logging.DEBUG)

# use a greenlet pool to cap the number of workers at a reasonable level
greenlet_pool = Pool(size=MAX_NUM_POOL_WORKERS)

def main():
    for message in queue.consume():
        greenlet_pool.spawn(worker, message)

def priceCheck(typeID):
    typeID = int(typeID)
    logging.debug("Updating mineral prices for %i" % (typeID))
    mc = pylibmc.Client([mcserver], binary=True, behaviors={"tcp_nodelay": True, "ketama": True})
    if mckey + "price" + str(typeID) in mc:
        return mc.get(mckey + "price" + str(typeID))
    # Handle DBs without password
    if not dbpass:
    # Connect without password
        pricedbcon = psycopg2.connect("host="+dbhost+" user="+dbuser+" dbname="+dbname+" port="+dbport)
    else:
        pricedbcon = psycopg2.connect("host="+dbhost+" user="+dbuser+" password="+dbpass+" dbname="+dbname+" port="+dbport)
    curs = pricedbcon.cursor()
    try:
        curs.execute("""select manual, override, api from killprices where typeid = %s""", (typeID,))
        data = curs.fetchone()
        if data[1]:
            return data[0]
    except:
        pass
    if psource == "psql":
        retVal = psqlpricing(typeID)
    elif psource == "ec":
        retVal = ecpricing(typeID)
    elif psource == "e43api":
        retVal = e43pricing(typeID)
    else:
        retVal = ecpricing(typeID)
    if retVal == None or retVal == 0.0 or not retVal:
        retVal = 0
    elif int(retVal) != 0:
        mc.set(mckey + "price" + str(typeID), retVal, 600)
        logging.debug("Updating mineral prices for %i to %f in database" % (typeID, retVal))
        try:
            curs.execute("""update killprices set api = %s where typeid = %s""", (retVal,typeID))
        except:
            curs.execute("""insert into killprices (typeid, api) values (%s, %s)""", (typeID, retVal))
        pricedbcon.commit()
    logging.debug("Item: %i Value: %f" % (typeID, retVal))
    return retVal

def ecpricing(typeID):
    conn = httplib.HTTPConnection(ecapi)
    conn.request("GET", "/api/marketstat/?typeid=%i&regionlimit=10000002" % (typeID))
    res = conn.getresponse()
    data = res.read()
    conn.close()
    root = ET.fromstring(data)
    for data in root.findall("./marketstat/type/sell/percentile"):
        retVal = float(data.text)
    try:
        if retVal == 0.0:
            for data in root.findall("./marketstat/type/sell/median"):
                retVal = float(data.text)
    except NameError:
        for data in root.findall("./marketstat/type/sell/median"):
            retVal = float(data.text)
    try:
        retVal
    except NameError:
        retVal = False
    if retVal == 0.0:
        retVal = False
    return retVal

def e43pricing(typeID):
    conn = httplib.HTTPConnection(e43api)
    conn.request("GET", "/market/api/marketstat/?typeid=%i&regionlimit=10000002" % (typeID))
    res = conn.getresponse()
    data = res.read()
    conn.close()
    root = ET.fromstring(data)
    for data in root.findall("./marketstat/type/sell/percentile"):
        retVal = float(data.text)
    try:
        if retVal == 0.0:
            for data in root.findall("./marketstat/type/sell/median"):
                retVal = float(data.text)
    except NameError:
        for data in root.findall("./marketstat/type/sell/median"):
            retVal = float(data.text)
    try:
        retVal
    except NameError:
        retVal = False
    if retVal == 0.0:
        retVal = False
    return retVal


def psqlpricing(typeID):
    # Handle DBs without password
    if not dbpass:
    # Connect without password
        dbcon = psycopg2.connect("host="+psqlhost+" user="+psqluser+" dbname="+psqlname+" port="+psqlport)
    else:
        dbcon = psycopg2.connect("host="+psqlhost+" user="+psqluser+" password="+psqlpass+" dbname="+psqlname+" port="+psqlport)
    curs = dbcon.cursor(cursor_factory=psycopg2.extras.DictCursor)
    curs.execute("""select * from market_data_itemregionstat where mapregion_id = 10000002 and invtype_id = %s """, (typeID,))
    try:
        data = curs.fetchone()
        if data['sell_95_percentile'] != 0:
            return data['sell_95_percentile']
        elif data['sellmedian'] != 0:
            return data['sellmedian']
    except:
        curs.execute("""select * from market_data_itemregionstathistory where mapregion_id = 10000002 and invtype_id = %s and (sellmedian != 0 or sell_95_percentile != 0) order by date desc limit 1""", (typeID,))
        try:
            data = curs.fetchone()
            if data['sell_95_percentile'] != 0:
                return data['sell_95_percentile']
            elif data['sellmedian'] != 0:
                return data['sellmedian']
        except:
            pass
    return False

def worker(message):
    # Handle DBs without password
    if not dbpass:
    # Connect without password
        dbcon = psycopg2.connect("host="+dbhost+" user="+dbuser+" dbname="+dbname+" port="+dbport)
    else:
        dbcon = psycopg2.connect("host="+dbhost+" user="+dbuser+" password="+dbpass+" dbname="+dbname+" port="+dbport)
    curs = dbcon.cursor()
    curs2 = dbcon.cursor()
    logging.debug("Pulling API vCode and Characters for keyID %i" % message)
    curs2.execute("""select id, keyid, vcode, charid, corp from killapi where id = %s and active = True""", (message,))
    for result in curs2:
        sqlid = result[0]
        key = result[1]
        vcode = result[2]
        charid = result[3]
        corp = result[4]
        if corp:
            curs.execute("""update killapi set updtime = now() + interval '1 hour 15 minutes' where id = %s""", (sqlid,))
        else:
            curs.execute("""update killapi set updtime = now() + interval '2 hours' where id = %s""", (sqlid,))
        dbcon.commit()
        logging.debug("Found character information.  KeyID: %s  charID: %s Corp: %s" % (key, charid, corp))
        api = eveapi.EVEAPIConnection()
        auth = api.auth(keyID=key, vCode=vcode)
        if corp:
            try:
                killAPI = auth.corp.KillLog(characterID=charid)
            except eveapi.Error, e:
                logging.info("Corp API Key %s for character %s had an issue during API access %s" % (key, charid, e.code))
                if 200 <= e.code <= 209:
                    logging.info("Corp API Key %s for character %s is disabled due to Authentication issues" % (key, charid))
                    curs.execute("""update killapi set active = False where id = %s""", (sqlid,))
                continue
        else:
            try:
                killAPI = auth.char.KillLog(characterID=charid)
            except eveapi.Error, e:
                logging.info("Char API Key %s for character %s had an issue during API access %s" % (key, charid, e.code))
                if 200 <= e.code <= 205:
                    logging.info("Char API Key %s for character %s is disabled due to Authentication issues" % (key, charid))
                    curs.execute("""update killapi set active = False where id = %s""", (sqlid,))
                continue
        try:
            for kill in killAPI.kills:
                pricesum = 0
                killid = kill.killID
                curs.execute("""select killid from killlist where killid = %s""", (killid,))
                try:
                    if curs.fetchone() != None:
                        continue
                except ProgrammingError:
                    pass
   
                curs.execute("""insert into killlist values (%s, %s, TIMESTAMPTZ 'epoch' + %s * '1 second'::interval, %s
                    )""", (killid, kill.solarSystemID, kill.killTime, kill.victim.characterID))

                logging.debug("Adding lost items...  KeyID: %s  charID: %s Corp: %s" % (key, charid, corp))

                for items in kill.items:
                    logging.debug("Item lost %s" % (items.typeID))
                    price = priceCheck(items.typeID)
                    pricesum += (price * items.qtyDropped) + (price * items.qtyDestroyed)
                    curs.execute("""insert into killitems values(%s, %s, %s, %s, %s, %s, %s)""", (killid, items.typeID,
                        items.flag, items.qtyDropped, items.qtyDestroyed, items.singleton, price))

                price = priceCheck(kill.victim.shipTypeID)
                pricesum += price
                logging.debug("Player Killed %s" % (kill.victim.characterName))
                curs.execute("""insert into killvictim values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""", (killid,
                    kill.victim.allianceID, kill.victim.allianceName, kill.victim.characterID, kill.victim.characterName,
                    kill.victim.corporationID, kill.victim.corporationName, kill.victim.damageTaken, kill.victim.factionID,
                    kill.victim.factionName, kill.victim.shipTypeID, price))

                for attackers in kill.attackers:
                    logging.debug("Attacker: %s" % (attackers.characterName))
                    curs.execute("""insert into killattackers values(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::boolean, %s, %s)
                        """, (killid, attackers.characterID, attackers.characterName, attackers.corporationID,
                        attackers.corporationName, attackers.allianceID, attackers.allianceName,
                        attackers.factionID, attackers.factionName, attackers.securityStatus, attackers.damageDone,
                        attackers.finalBlow, attackers.weaponTypeID, attackers.shipTypeID))

                logging.debug("Final Price: %s KillID: %s" % (pricesum, killid))
                curs.execute("""update killlist set price = %s where killid = %s""", (pricesum, killid))

                dbcon.commit()
        except Exception, err:
            print err
            return

if __name__ == '__main__':
    main()
