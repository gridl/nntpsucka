#!/usr/bin/env python
#
# Copyright (c) 2002  Dustin Sallings <dustin@spy.net>
#
# arch-tag: 8F42F3DA-10C0-11D9-A55D-000393CFE6B8

from sqlite3 import dbapi2 as sqlite
import ConfigParser
import Queue
import datetime
import logging
import logging.config
import nntplib
import os
import re
import signal
import sys
import threading
import time
import traceback

# My pidlock
import pidlock

# Configuration defaults
CONF_DEFAULTS={'port':'119', 'newsdb':'newsdb', 'pidfile':'nntpsucka.pid',
    'shouldMarkArticles': 'true', 'maxArticles': 0}
CONF_SECTIONS=['misc', 'servers']

class Stats:
    """Keep statistics describing what got moved."""

    def __init__(self):
        self.moved=0
        self.dup=0
        self.other=0

    def addMoved(self):
        """Mark one article copied."""
        self.moved+=1

    def addDup(self):
        """Mark one article duplicate (not copied)."""
        self.dup+=1

    def addOther(self):
        """Mark one article not copied, but not duplicate."""
        self.other+=1

    def __str__(self):
        return "Moved:  " + str(self.moved) \
            + ", duplicate:  " + str(self.dup) \
            + ", Other:  " + str(self.other)

######################################################################

DBINITSCRIPT="""
create table if not exists articles (
    messid varchar(256) primary key,
    ts timestamp
);

create table if not exists groups (
    group_name varchar(256) primary key,
    last_id int
);
"""

INS_ARTICLE="""insert or replace into articles values(?, ?)"""
GET_ARTICLE="""select * from articles where messid = ?"""
INS_GROUP="""insert or replace into groups values (?, ?)"""
GET_GROUP="""select * from groups where group_name = ?"""

class NewsDB:
    """Database of seen articles and groups.

    Entries for groups will have keys beginning with ``l/'' and entries for
    individual articles will have keys beginning with ``a/''

    Article entries (a) will have a string value that's a floating point number
    indicating the time that this article was copied.

    Group entries (l) have a string value that represents that last seen
    article by number for the given group (the part after the l/).
    """

    __TXN_SIZE = 100

    def __init__(self, dbpath):
        self.db = sqlite.connect(dbpath)
        self.cur = self.db.cursor()
        self.cur.executescript(DBINITSCRIPT)
        self.log = logging.getLogger("NewsDB")
        self.__trans = 0
        self.__markArticles = True

    def __maybeCommit(self):
        self.__trans += 1
        if self.__trans >= self.__TXN_SIZE:
            self.__trans = 0
            self.db.commit()

    def setShouldMarkArticles(self, to):
        """Set to false if articles should not be marked in the news db."""
        self.__markArticles=to

    def hasArticle(self, message_id):
        """Return true if there is a reference to the given message ID in
        this database.
        """
        rv=False
        if self.__markArticles:
            self.cur.execute(GET_ARTICLE, (message_id,))
            rv = len(self.cur.fetchall()) > 0
        return rv

    def markArticle(self, message_id):
        """Mark the article seen."""
        if self.__markArticles:
            self.cur.execute(INS_ARTICLE, (message_id, datetime.datetime.now()))
            self.__maybeCommit()

    def getLastId(self, group):
        """Get the last seen article ID for the given group, or 0 if this
        group hasn't been seen.
        """
        rv=0
        self.cur.execute(GET_GROUP, (group,))
        rows = self.cur.fetchall()
        if len(rows) > 1:
            rv = int(rows[0][1])
        return rv

    def setLastId(self, group, id):
        """Set the last seen article ID for the given group."""
        self.cur.execute(INS_GROUP, (group, id))
        self.__maybeCommit()

    def __del__(self):
        """Close the DB on destruct."""
        self.db.commit()
        self.cur.close()
        self.db.close()

    def getGroupRange(self, group, first, last, maxArticles=0):
        """Get the group range for the given group.

        The arguments represent the group you're looking to copy, and the
        first and last article numbers as provided by the news server.

        The first, last, and count that should be checked will be returned
        as a tuple."""

        # Start with myfirst being one greater than the last thing we've seen
        myfirst=self.getLastId(group) + 1
        first=int(first)
        last=int(last)
        self.log.debug("%s ranges from %d-%d, we want %d\n" \
            % (group, first, last, myfirst))
        if (myfirst < first) or (myfirst > (last+1)):
            myfirst=first
            self.log.debug("Our first was out of range, now we want %d\n" \
                % (myfirst, ))
        mycount=(last-myfirst)+1

        self.log.debug("Want no more than %d articles, found %d from %d\n"
            % (maxArticles, mycount, myfirst))

        if maxArticles > 0 and mycount > maxArticles:
            self.log.debug("Want %d articles with a max of %d...shrinking\n" \
                % (mycount, maxArticles))
            myfirst = myfirst + (mycount - maxArticles)
            mycount = maxArticles
            self.log.debug("New count is %d, starting with %s"
                % (mycount, myfirst))

        return myfirst, last, mycount

######################################################################

class NNTPClient(nntplib.NNTP):
    """An extension of nntplib.NNTP suitable for...well, it actually
    works."""

    headers=['From', 'Subject', 'Message-Id', 'Sender', 'MIME-Version', \
        'Path', 'Newsgroups', 'Organization', 'Approved', 'Sender', \
        'Distribution', \
        'Lines', 'Content-Type', 'Content-Transfer-Encoding']

    def __init__(self, host, port=119,user=None,password=None,readermode=None):
        """See netlib.NNTP"""
        self.log=logging.getLogger("NNTPClient")
        self.log.info("Connecting to %s:%d" % (host, port))
        nntplib.NNTP.__init__(self, host, port, user, password, readermode)
        self.log.debug("Connected to %s:%d" % (host, port))
        self.checkMode()
        self.host=host
        self.port=port

    def __repr__(self):
        return ("<NNTPClient: " + self.host + ":" + `self.port` + ">")

    def checkMode(self):
        """Upon construct, this tries to figure out whether the server
        considers us a feeder or a reader."""
        try:
            self.group('control')
            self.currentmode='reader'
        except nntplib.NNTPPermanentError:
            self.currentmode='poster'
        except nntplib.NNTPTemporaryError:
            self.currentmode='poster'
        self.log.debug("Detected mode %s" % (self.currentmode))

    def __headerMatches(self, h):
        """Internal, checks to see if the header ``h'' is in our list of
        approved headers."""
        rv=None
        for header in self.headers:
            if h.lower().find(header.lower()) == 0:
                rv=1
        return rv

    def ihave(self, id):
        """Send an IHAVE for a message ID (feeder mode only).

        An exception will be raised if something breaks, or the article
        isn't wanted.  If an exception is not raised, follow with a stream
        of the article."""
        self.log.debug("IHAVing " + id)
        resp = self.shortcmd('IHAVE ' + id)
        self.log.debug("IHAVE returned " + str(resp))

    def copyArticle(self, src, which, messid):
        """Copy an article from the src server to this server.

        which is the ID of the message on the src server, messid is the
        message ID of the article."""
        self.log.debug("Moving " + str(which))
        if self.currentmode == 'reader':
            resp, nr, id, lines = src.article(str(which))
            self.post(lines)
        else:
            self.ihave(messid)
            try:
                resp, nr, id, lines = src.article(messid)
            except nntplib.NNTPTemporaryError, e:
                # Generate an error, I don't HAVE this article, after all
                self.log.warn("Did not have %s", messid)
                try:
                    self.shortcmd('\r\n.')
                except nntplib.NNTPTemporaryError, e:
                    return
            self.takeThis(messid, lines)

    def takeThis(self, messid, lines):
        """Stream an article to this server."""
        self.log.debug("*** TAKE THIS! ***")
        for l in lines:
            if l == '.':
                self.log.debug("*** L was ., adding a dot. ***")
                l = '..'
            self.putline(l)
        self.putline('.')
        self.getresp()

    def post(self, lines):
        """Post an article to this server."""
        self.log.debug("*** POSTING! ***")
        resp = self.shortcmd('POST')
        if resp[0] != '3':
            raise nntplib.NNTPReplyError(resp)
        headers=1
        for l in lines:
            if l == '':
                headers=None
                self.putline('')
            else:
                if headers:
                    if self.__headerMatches(l):
                        self.putline(l)
                else:
                    if l == '.':
                        self.log.debug("*** L was ., adding a dot. ***")
                        l = '..'
                    self.putline(l)
        self.putline('.')
        self.getresp()

######################################################################

class Worker(threading.Thread):

    def __init__(self, sf, df, inq, outq):
        threading.Thread.__init__(self)
        self.srcf = sf
        self.destf = df
        self.inq = inq
        self.outq = outq
        self.log=logging.getLogger("Worker")
        self.currentGroup = ""
        self.running = True

        self.setName("worker")
        self.setDaemon(True)
        self.start()

    def run(self):
        while self.running:
            try:
                self.src = self.srcf()
                self.dest = self.destf()
                self.mainLoop()
            except nntplib.NNTPPermanentError, e:
                traceback.print_exc()
                try:
                    self.src.quit()
                except:
                    pass
                try:
                    self.dest.quit()
                except:
                    pass
            except:
                traceback.print_exc()
                sys.exit(1)

    def mainLoop(self):
        while self.running:
            group, num, messid = self.inq.get()
            self.log.debug("doing %s, %s, %s", group, num, messid)
            if group != self.currentGroup:
                self.src.group(group)
                self.currentGroup = group
            try:
                self.dest.copyArticle(self.src, num, messid)
                self.outq.put(('success', messid))
            except nntplib.NNTPTemporaryError, e:
                if str(e).find("Duplicate"):
                    self.outq.put(('duplicate', messid))
                else:
                    self.outq.put(('error', messid))
                    self.log.warn("Failed:  " + str(e))

            self.inq.task_done()

class NNTPSucka:
    """Copy articles from one NNTP server to another."""

    def __init__(self, srcf, destf, config):
        """Get an NNTPSucka with two NNTPClient objects representing the
        source and destination."""
        self.log=logging.getLogger("NNTPSucka")
        self.src=srcf()
        self.dest=destf()

        self.reqQueue = Queue.Queue(1000)
        self.doneQueue = Queue.Queue(1000)

        # Figure out the maximum number of articles per group
        self.maxArticles=config.getint("misc", "maxArticles")
        self.log.debug("Max articles is configured as %d" %(self.maxArticles))

        # NewsDB setup
        self.db=NewsDB(config.get("misc","newsdb"))
        self.db.setShouldMarkArticles(config.getboolean("misc",
            "shouldMarkArticles"))

        # Initialize stats
        self.stats=Stats()

        self.workers = [Worker(srcf, destf, self.reqQueue, self.doneQueue)
                        for x in range(config.getint("misc", "workers"))]

    def copyGroup(self, groupname):
        """Copy the given group from the source server to the destination
        server.

        Efforts are made to ensure only articles that haven't been seen are
        copied."""
        self.log.debug("Getting group " + groupname + " from " + `self.src`)
        resp, count, first, last, name = self.src.group(groupname)
        self.log.debug("Done getting group")

        # Figure out where we are
        myfirst, mylast, mycount= self.db.getGroupRange(groupname, first, last,
            self.maxArticles)
        l=[]
        if mycount > 0:
            self.log.info("Copying " + `mycount` + " articles:  " \
                + `myfirst` + "-" + `mylast` + " in " + groupname)

            # Grab the IDs
            resp, l = self.src.xhdr('message-id', `myfirst` + "-" + `mylast`)

            # Validate we got as many results as we expected.
            if(len(l) != mycount):
                self.log.warn("Unexpected number of articles returned.  " \
                    + "Expected " + `mycount` + ", but got " + `len(l)`)

        # Flip through the stuff we actually want to process.
        for i in l:
            try:
                t,  messid = self.doneQueue.get_nowait()
                if t == 'success':
                    self.log.debug("Finished %s", messid)
                    self.db.markArticle(messid)
                    self.stats.addMoved()
                elif t == 'duplicate':
                    self.db.markArticle(messid)
                    self.stats.addDup()
                else:
                    self.stats.addOther()
            except Queue.Empty:
                pass
            try:
                messid="*empty*"
                messid=i[1]
                idx=i[0]
                self.log.debug("idx is " + idx + " range is " + `myfirst` \
                    + "-" + `mylast`)
                assert(int(idx) >= myfirst and int(idx) <= mylast)
                if self.db.hasArticle(messid):
                    self.log.info("Already seen " + messid)
                    self.stats.addDup()
                else:
                    self.reqQueue.put((groupname, idx, messid))
                # Mark this message as having been read in the group
                self.db.setLastId(groupname, idx)
            except KeyError, e:
                # Couldn't find the header, article probably doesn't
                # exist anymore.
                pass

    def shouldProcess(self, group, ignorelist):
        rv = True
        for i in ignorelist:
            if i.match(group) is not None:
                rv = False
        return rv

    def copyServer(self, ignorelist=[]):
        """Copy all groups that appear on the destination server to the
        destination server from the source server."""
        self.log.debug("Getting list of groups from " + `self.dest`)
        resp, list = self.dest.list()
        self.log.debug("Done getting list of groups from destination")
        for l in list:
            group=l[0]
            if self.shouldProcess(group, ignorelist):
                try:
                    self.log.debug("copying " + `group`)
                    self.copyGroup(group)
                except nntplib.NNTPTemporaryError, e:
                    self.log.warn("Error on group " + group + ":  " + str(e))

        self.reqQueue.join()

    def getStats(self):
        """Get the statistics object."""
        return self.stats

class OptConf(ConfigParser.ConfigParser):
    """ConfigParser with get that supports default values"""

    def __init__(self, defaults=None, sections=[]):
        ConfigParser.ConfigParser.__init__(self, defaults)
        for s in sections:
            if not self.has_section(s):
                self.add_section(s)

    def getWithDefault(self, section, option, default, raw=False, vars=None):
        """returns the configuration entry, or the ``default'' argument"""
        rv=default
        try:
            rv=self.get(section, option, raw, vars)
        except ConfigParser.NoSectionError:
            pass
        except ConfigParser.NoOptionError:
            pass
        return rv

class Timeout:
    """This is an exception that's raised when the alarm handler fires."""
    pass

def alarmHandler(sig, frame):
    """Do nothing but raise a timeout."""
    raise Timeout

def getIgnoreList(fn):
    log=logging.getLogger("nntpsucka")
    log.debug("Getting ignore list from " + fn)
    rv=[]
    f=open(fn)
    for l in f.readlines():
        l=l.strip()
        rv.append(re.compile(l))
    return rv

def connectionMaker(conf, which):

    connServer=conf.get("servers", which)
    connUser=None
    connPass=None
    connPort=119
    num_conn = 1
    if conf.has_section(connServer):
        connUser=conf.getWithDefault(connServer, "username", None)
        connPass=conf.getWithDefault(connServer, "password", None)
        connPort=conf.getint(connServer, "port")

    def f():
        return NNTPClient(connServer, port=connPort,
                          user=connUser, password=connPass)

    return f

def main():
    conf=OptConf(CONF_DEFAULTS, CONF_SECTIONS)
    conf.read(sys.argv[1])

    # Lock to make sure only one is running at a time.
    lock=pidlock.PidLock(conf.get("misc", "pidfile"))

    # How long do we wait for startup?
    signal.signal(signal.SIGALRM, alarmHandler)
    signal.alarm(30)
    # And how long will we wait for the actual processing?
    TIMEOUT = 4 * 3600

    # Validate there's a config file
    if len(sys.argv) < 2:
        sys.stderr.write("Usage:  " + sys.argv[0] + " configFile\n")
        sys.exit(1)

    # Configure logging.
    logging.config.fileConfig(sys.argv[1])

    filterList=conf.getWithDefault("misc", "filterList", None)

    fromFactory = connectionMaker(conf, "from")
    toFactory = connectionMaker(conf, "to")

    sucka=None
    # Mark the start time
    start=time.time()
    try:
        ign=[re.compile('^control\.')]
        if filterList is not None:
            ign=getIgnoreList(filterList)
        signal.alarm(30)
        sucka=NNTPSucka(fromFactory, toFactory, config=conf)
        signal.alarm(TIMEOUT)
        sucka.copyServer(ign)
    except Timeout:
        sys.stderr.write("Took too long.\n")
        sys.exit(1)
    # Mark the stop time
    stop=time.time()

    if sucka:
        # Log the stats
        log=logging.getLogger("nntpsucka")
        log.info(sucka.getStats())
        log.info("Total time spent:  " + str(stop-start) + "s")

if __name__ == '__main__':
    try:
        main()
    except pidlock.AlreadyLockedException, ale:
        sys.stderr.write("Already running:  " + str(ale[0]) + "\n")
