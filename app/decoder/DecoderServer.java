import com.quanta.g21.java.rsm.model.core.handler.video.VideoController;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.io.EOFException;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;

/**
 * Persistent decoder for the BMC video stream.
 * Protocol (big-endian): int32 length, frame bytes (32-byte header + payload).
 * Response: int32 width, int32 height, int32 boxes, then width*height*3 BGR bytes.
 */
public class DecoderServer {
    public static void main(String[] args) throws Exception {
        if (args.length < 2) {
            System.err.println("usage: DecoderServer <dll> <port>");
            System.exit(2);
        }
        System.load(new java.io.File(args[0]).getAbsolutePath());
        int port = Integer.parseInt(args[1]);
        ServerSocket server = new ServerSocket(port, 1, InetAddress.getByName("127.0.0.1"));
        System.err.println("decoder listening on 127.0.0.1:" + port);
        int session = 0;
        while (true) {
            Socket sock = server.accept();
            sock.setTcpNoDelay(true);
            session++;
            String key = "gui-" + session;
            VideoController decoder = new VideoController(key);
            DataInputStream in = new DataInputStream(new BufferedInputStream(sock.getInputStream()));
            DataOutputStream out = new DataOutputStream(new BufferedOutputStream(sock.getOutputStream()));
            byte[] pixels = null;
            int[] boxes = null;
            int width = 0;
            int height = 0;
            try {
                while (true) {
                    int len;
                    try {
                        len = in.readInt();
                    } catch (EOFException eof) {
                        break;
                    }
                    if (len <= 32 || len > 8 * 1024 * 1024) {
                        continue;
                    }
                    byte[] frame = new byte[len];
                    in.readFully(frame);
                    int w = (frame[12] & 0xff) | ((frame[13] & 0xff) << 8);
                    int h = (frame[14] & 0xff) | ((frame[15] & 0xff) << 8);
                    if (w <= 0 || h <= 0 || w > 4096 || h > 4096) {
                        continue;
                    }
                    if (pixels == null || w != width || h != height) {
                        width = w;
                        height = h;
                        pixels = new byte[w * h * 4 + 32];
                        boxes = new int[w * h + 1];
                    }
                    decoder.decodePart(key, frame, pixels, boxes, len);
                    out.writeInt(width);
                    out.writeInt(height);
                    out.writeInt(boxes[0]);
                    out.write(pixels, 0, width * height * 3);
                    out.flush();
                }
            } catch (Exception exc) {
                System.err.println("session " + key + " ended: " + exc);
            } finally {
                try { sock.close(); } catch (Exception ignored) { }
            }
        }
    }
}
