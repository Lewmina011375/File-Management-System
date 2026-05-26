# Access Northsail from another laptop (LAN)

## On this laptop (where the app runs)

1. Connect both laptops to the **same Wi‑Fi or same network** (same router/switch).
2. In a terminal, go to the project folder and run:
   ```bash
   cd northsale
   python app.py
   ```
3. When the app starts, it will print something like:
   ```
   --------------------------------------------------
   Northsail running.
     On this PC:     http://127.0.0.1:5000
     From other PC: http://192.168.1.105:5000
   --------------------------------------------------
   ```
4. Note the **"From other PC"** URL (your LAN IP and port 5000).

## On the other laptop

1. Open a browser (Chrome, Edge, etc.).
2. Type the **"From other PC"** address, e.g. `http://192.168.1.105:5000`.
3. You should see the Northsail app.

## If it doesn’t connect

- **Windows Firewall:** When you first run the app, Windows may ask to allow Python. Choose **Private networks** (or allow access).
- **Same network:** Both laptops must be on the same Wi‑Fi/LAN (same router).
- **Correct IP:** The IP in the URL is for *this* laptop. Use that exact address on the *other* laptop.

## Summary

| Where you are      | URL to use                    |
|--------------------|-------------------------------|
| This PC (host)     | http://127.0.0.1:5000        |
| Other PC on LAN    | http://YOUR_IP_HERE:5000     |

Replace `YOUR_IP_HERE` with the IP shown in the "From other PC" line when you start the app.
