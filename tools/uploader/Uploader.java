import java.io.BufferedReader;
import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;

public class Uploader {
    public static void main(String[] args) {
        try {
            if (args.length < 2) {
                System.out.println("usage: Uploader <file> <x-token> [name]");
                return;
            }
            File f = new File(args[0]);
            String token = args[1];
            String name = args.length > 2 ? args[2] : f.getName();
            String boundary = "----z" + System.currentTimeMillis();
            URL u = new URL("http://pc.tjphone.cloud/api/file/upload");
            HttpURLConnection c = (HttpURLConnection) u.openConnection();
            c.setDoOutput(true);
            c.setRequestMethod("POST");
            c.setConnectTimeout(15000);
            c.setReadTimeout(120000);
            c.setRequestProperty("X-Token", token);
            c.setRequestProperty("platform", "desktop");
            c.setRequestProperty("Content-Type", "multipart/form-data; boundary=" + boundary);
            OutputStream o = c.getOutputStream();
            write(o, ("--" + boundary + "\r\nContent-Disposition: form-data; name=\"file\"; filename=\"" + name + "\"\r\nContent-Type: application/octet-stream\r\n\r\n").getBytes("UTF-8"));
            InputStream in = new FileInputStream(f);
            byte[] buf = new byte[65536];
            int n;
            long total = 0;
            while ((n = in.read(buf)) > 0) { o.write(buf, 0, n); total += n; }
            in.close();
            write(o, ("\r\n--" + boundary + "--\r\n").getBytes("UTF-8"));
            o.close();
            int code = c.getResponseCode();
            InputStream is = code >= 400 ? c.getErrorStream() : c.getInputStream();
            BufferedReader r = new BufferedReader(new InputStreamReader(is, "UTF-8"));
            StringBuilder sb = new StringBuilder();
            String line;
            while ((line = r.readLine()) != null) sb.append(line);
            System.out.println("HTTP " + code + " sent=" + total + "B resp=" + sb);
        } catch (Exception e) {
            System.out.println("ERR " + e);
        }
    }

    static void write(OutputStream o, byte[] d) throws IOException { o.write(d); }
}
