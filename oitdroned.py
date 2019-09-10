#!/usr/bin/python3
import sys
import time
import socket
import argparse
import threading
import http.server
import concurrent.futures
import numpy as np
import cv2
from functools import reduce


# Tello 関係
ADDR_TELLO = ("192.168.10.1", 8889)
ADDR_RESP = ("", 8889)
#ADDR_RESP = ("", 9000)
ADDR_STATE = ("0.0.0.0", 8890)
recvQ = []
cmdQ = []    # [(scratch_id, cmd_string), ...] queue for blocking commands
tello_state = {}
NONBLOCKING_COMMANDS = ["rc"]
NONTELLO_COMMANDS = ["capture"]

# ビジョン関係
PORT = 10105   # CV = 105
AreaThreshold = 80   #  これだけピクセル以下は検出しない
capres = []          # [[#, device, x, y, area, nFound], ...]
capstream = None     # video stream capture
captureFilename = None  # cmd threadでセット→CV threadで保存しNoneに (複雑!)
frameStream = []

# いろいろ共用
lock = threading.Lock()

def threadRecvResponse(sock):
    """
    Tello からレスポンスを受け取って recvQ に入れるスレッドよう
    :param socket.socket sock: ソケット
    """
    global lock, recvQ
    while True:
        try:
            data, (addr, port) = sock.recvfrom(1518)
            lock.acquire()
            recvQ.append(data.decode(encoding="utf-8"))
            lock.release()
        except Exception as e:
            print("threadRecvResponse", e)
            break

def threadRecvState(sock):
    """
    Tello からのテレメトリ受信。tello_state にハッシュとして格納。
    :param socket.socket sock: ソケット
    """
    global lock, tello_state
    while True:
        try:
            data, (addr, port) =  sock.recvfrom(1518)
            msg = data.decode(encoding="utf-8").split(";")
            lock.acquire()
            for i in msg:
                try:
                    key, val = tuple(i.split(":"))
                    tello_state[key] = val
                except Exception:
                    pass
            lock.release()
        except Exception as e:
            print("threadRecvState: ", e)
            break
        else:
            break

def threadBlockingCommand(sock):
    """
    cmdQ にためられたコマンドを一つずつ実行する。
    """
    global lock, cmdQ, captureFilename
    while True:
        if len(cmdQ) > 0:
            #print("doing: ", end="")
            #print(cmdQ[0])
            if int(cmdQ[0][0]) < 0:
                sendTelloNonblock(sock, cmdQ[0][1])
            else:
                theCmd = cmdQ[0][1].split(" ")
                if theCmd[0] in NONTELLO_COMMANDS:
                    if theCmd[0] == "capture":
                        lock.acquire()
                        captureFilename = theCmd[1]
                        lock.release()
                        while captureFilename != None:
                            print("waiting")
                            time.sleep(0.2)  #CV スレッドで保存待ち
                else:
                    print(sendTello(sock, cmdQ[0][1]))
            lock.acquire()
            cmdQ = cmdQ[1:]
            lock.release()
        else:
            time.sleep(0.1)
        

        
def sendTelloNonblock(sock, cmd):
    """
    Telloにコマンドを送る。ブロッキングしない。
    :param socket.socket sock: socket
    :param String cmd: コマンド文字列
    """
    print("%s > Tello" % (cmd))
    sock.sendto(cmd.encode(encoding="utf-8"), ADDR_TELLO)
    sock.sendto(cmd.encode(encoding="utf-8"), ADDR_TELLO)
    sock.sendto(cmd.encode(encoding="utf-8"), ADDR_TELLO)

def sendTello(sock, cmd):
    """
    Telloにコマンドを送る。ok/error 待ちブロッキングする。
    :param socket.socket sock: socket
    :param String cmd: コマンド文字列
    :rtype: String
    :return: ok/error/timeout
    """
    global lock
    global recvQ
    lock.acquire()
    recvQ = []
    lock.release()
    sendTelloNonblock(sock, cmd)
    for i in range(150):   # 0.1 * 150 = 15 s でタイムアウト
        time.sleep(0.1)
        lock.acquire()
        resp = None if len(recvQ) == 0 else recvQ[-1]
        lock.release()
        if resp != None:
            if cmd[-1] == "?" or resp[:2] == "ok":
                return resp
    return "timeout"

    

class HTTPServer(http.server.BaseHTTPRequestHandler):
    """
    the interface to Scratch (2.x)
    commands other than "poll" are queued into cmdQ, while "poll"
    (all responsive commands such as reading sensor are results of "poll")
    is processed here.
    """
    def do_GET(self):
        global lock, capres, cmdQ
        args = self.path.split("/")
        while args[0] == "":
            args = args[1:]
        if args[0] == "poll":
            lock.acquire()
            self.send_response(200, "OK")
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            resp0 = ""
            resp1 = ""
            respn0 = ""
            respn1 = ""
            telemetry = ""
            busy = "_busy"
            try:
                resp0  = "x0 %d\x0ay0 %d\x0a" % (capres[0][2], capres[0][3])
                respn0 = "nf0 %d\0a" % (capres[0][5])
                resp1  = "x1 %d\x0ay1 %d\x0a" % (capres[1][2], capres[1][3])
                respn1 = "nf1 %d\0a" % (capres[1][5])
            except Exception:
                pass
            for k in tello_state:
                telemetry = telemetry + k + " " + tello_state[k] + "\x0a"
            for i in cmdQ:
                if int(i[0]) >= 0:
                    busy = busy + " " + str(i[0])
            busy = busy + "\x0a"
            lock.release()
            self.wfile.write(bytes(resp0 + resp1 + respn0 + respn1 + telemetry + busy, "utf-8"))
        elif args[0] == "reset_all":
            lock.acquire()
            cmdQ =  [(0, "land")]
            lock.release()
        else:
            if args[0] in NONBLOCKING_COMMANDS:
                cmd_str = reduce(lambda x, y: x + " " + y, args)
                lock.acquire()
                cmdQ.append((-1, cmd_str))  # ブロッキングしない場合 id = -1
                lock.release()
            else:
                cmd = args[0]        # "/cmd/id/params" from Scratch
                id = int(args[1])
                for (i, _) in cmdQ:  # already queued?
                    if int(i) == id:
                        cmd = None
                if cmd != None:
                    for i in args[2:]:  # /cmd/id/param1/param2/... なので
                        cmd = cmd + " " + str(i)
                    lock.acquire()
                    cmdQ.append((id, cmd))
                    lock.release()
            
    def log_message(self, format, *args): # override for silence
        pass


def threadHTTP():
    print("threadHTTP start")
    addr = ("", PORT)
    server = http.server.socketserver.TCPServer(addr, HTTPServer)
    server.serve_forever()

def threadStreaming(cap):
    global frameStream
    while True:
        cap.grab()
        r, f = cap.read()
        if r:
            frameStream.append(f)
        time.sleep(0.01)

#
# ここからメイン
#
parser = argparse.ArgumentParser(description="OIT Tello + Scratch backend server")
parser.add_argument('--camera', default=0, type=int)
args = parser.parse_args()

#for i in range(4):    # 複数台カメラサポート(今回はやらない)
#    c = cv2.VideoCapture(i)
#    if c.isOpened():
#        capres.append([i, c, -1, -1, -1])

c = cv2.VideoCapture(int(args.camera))
if c.isOpened():
    capres.append([int(args.camera), c, -1, -1, -1, 0])
else:
    print("Cannot open the VideoCapture device.")
#    sys.exit(-1)

with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor,\
     socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock_cmd,\
     socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock_state:
    sock_state.bind(ADDR_STATE)
    executor.submit(threadRecvState, sock_state)   # 状態受信
    sock_cmd.bind(ADDR_RESP)
    executor.submit(threadBlockingCommand, sock_cmd)
    executor.submit(threadRecvResponse, sock_cmd)  # ok等受信
    print(sendTello(sock_cmd, "command"))
    print(sendTello(sock_cmd, "streamon"))
    executor.submit(threadHTTP)                        # Scratch よう
    capstream = cv2.VideoCapture("udp://0.0.0.0:11111", cv2.CAP_ANY)
    if not capstream.isOpened():
        print("Cannot open Tello video stream.")
    else:
        executor.submit(threadStreaming, capstream)
    # cv関連はメインスレッドで
    while cv2.waitKey(1) != 27:
        if len(frameStream) > 0:
            theFrame = frameStream.pop()
            cv2.imshow("stream", theFrame)
            frameStream = []
            if captureFilename != None:
                cv2.imwrite(captureFilename, theFrame)
                cv2.imshow(captureFilename, theFrame)
                print("captured: ", captureFilename)
                lock.acquire()
                captureFilename = None
                lock.release()
        for the_capres in capres:
            camno = the_capres[0]
            cap = the_capres[1]
            if not cap.grab():
                print("not ready")
                continue
            ret, img0 = cap.read()
            height, width, channel = img0.shape

            img = cv2.GaussianBlur(img0, ksize=(5,5), sigmaX=1)
            hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
            lower = np.array([-5, 150, 150])
            upper = np.array([55, 255, 255])
            mask = cv2.inRange(hsv, lower, upper)
            mask = cv2.erode(mask, np.ones((2,2), dtype=np.uint8))
            mask = cv2.erode(mask, np.ones((2,2), dtype=np.uint8))

            labels = cv2.connectedComponentsWithStats(mask, connectivity=4)
            # targets <- [labels| 面積が閾値以上のもの]
            targets = filter(lambda l: l[0][4] >= AreaThreshold,
                             list(zip(labels[2], labels[3]))[1:])
            targets = sorted(targets, key=lambda x: -x[0][4])  # 面積でソート
            lock.acquire()
            the_capres[5] = len(targets)
            if len(targets) > 0:
                the_capres[2] = targets[0][1][0]  # 最大面積の x
                the_capres[3] = targets[0][1][1]  # 最大面積の y
                the_capres[4] = targets[0][0][4]  # 最大面積の 面積
            else:
                the_capres[2] = -1
                the_capres[3] = -1
                the_capres[4] = -1
            lock.release()
            for (rect, center) in targets:
                cv2.rectangle(img0, (rect[0], rect[1]),
                              (rect[0] + rect[2], rect[1] + rect[3]),
                              (255, 0, 0), 1)
                cv2.putText(img0,
                            "%d, %d (%d)" % (center[0], center[1], rect[4]),
                            (rect[0] + rect[2], rect[1] + rect[3]),
                            cv2.FONT_HERSHEY_PLAIN, 2, (255, 0, 0))
    #            print("%4d %4d  " % (center[0], center[1]), end="")
    #        print()
            cv2.imshow("camera %d" % the_capres[0], img0)
#        for i in capres:
#            print(i)
#        print()
