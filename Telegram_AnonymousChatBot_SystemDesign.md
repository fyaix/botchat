
# 🤖 Telegram Anonymous Chat Bot – Full System Design

## 🧩 1. Overview
A secure, smart, and anonymous random chat bot for Telegram with advanced filtering, user reputation, and human-based validation. Includes maintenance management for reliability and control.

---

## 💬 2. Core Features

| Feature | Description |
|----------|--------------|
| 🔄 Random Chat | Connect users anonymously and randomly. |
| 🧍 /next | Skip current partner and find a new one. |
| 🧱 /stop | End the current chat. |
| 🪪 /myid /showid | Display user's unique ID for manual contact. |
| 📎 Send Media | All users can send text, media, and stickers. |
| 🔒 Smart Filter | Detect vulgar words, suspicious links, and sticker spam. |
| 🧠 Behavior Analysis | Detect users who spam and skip too quickly. |
| 🏅 Premium Mode | Lets users choose chat preferences (gender, age, region). |
| 🕵️ Validation System | Ask user confirmation when partner sends vulgar promotions. |

---

## 💎 3. Premium vs Non-Premium

| Aspect | Non-Premium | Premium |
|---------|--------------|----------|
| Random Match | Fully random | Filtered by preferences |
| ID Sharing | Yes (/myid) | Yes |
| Media Access | Yes | Yes |
| Filter Protection | Same for all | Same |
| Match Priority | Normal | High |
| Starting Reputation | 80 | 100 |

---

## 🛡️ 4. Smart Filter & Safety System

### 🔹 Layers of Protection
1. **Content Filter** – Detect vulgar, promotion, and @username links.
2. **Sticker Filter** – Detect banned sticker IDs (auto-blacklist on reports).
3. **Delay Protection** – Delay first message for 3 seconds to scan for suspicious content.
4. **Behavior Analysis** – Detect users who send a message & skip within 5 seconds.
5. **Shadow Ban** – Isolate repeated offenders into dummy chats.
6. **Auto Report Linkage** – Link user reports to analyze repeating patterns.

### 🧠 Reputation System

| Reputation | Status | Effects |
|-------------|---------|----------|
| 100–80 | Normal | All features active |
| 79–60 | Monitored | 3s delay before sending messages |
| 59–40 | Risky | Cannot send stickers |
| <40 | Suspended | Cannot search for partner |

---

## 👮 5. Human Validation System

### 🔹 Trigger
If a user sends suspicious content and immediately skips, the system flags them and asks their partner for confirmation.

### 🔹 Confirmation Message
> ⚠️ Partner left after sending suspicious message.  
> Was it a vulgar or promotional message?

Buttons:
- ✅ Yes  
- ❌ No

### 🔹 System Response
| Answer | Action |
|---------|--------|
| ✅ Yes | Add violation points (+3), possibly auto-ban |
| ❌ No | No penalty |

---

## ⚙️ 6. Behavior Tracking & Reputation

| Behavior | Impact |
|-----------|--------|
| Normal chat >1 min | +5 |
| Reported once | -3 |
| Suspicious message | -2 |
| Confirmed spam | -5 |
| False report | -2 |

---

## 🔧 7. Maintenance Management System

### 🧱 Architecture Overview

```
[ Cloudflare Worker / Controller ]
          ↓
[ Railway Bot (Main Engine) ]
          ↓
[ Telegram Users ]
```

### 🔹 Roles

| Component | Function |
|------------|-----------|
| **Cloudflare Worker** | Acts as a smart proxy controlling maintenance mode. |
| **Railway Bot** | Handles real chat logic. |
| **Admin (Owner)** | Can toggle maintenance remotely. |

---

### 🔹 Normal Mode
1. Worker forwards Telegram updates to Railway.
2. Railway bot processes and replies.
3. Worker passes response back to Telegram.

### 🔹 Maintenance Mode
1. Worker detects `maintenance = true`.
2. Stops forwarding to Railway.
3. Returns message:
   > 🔧 Bot is under maintenance. Please try again later.

### 🔹 Owner Commands
- `/maintenance (message)` → Enables maintenance mode with custom message.
- `/resume` → Disables maintenance mode.
- `/shutdown` → Gracefully stops Railway bot process.

### 🔹 Data Model (in KV or JSON)

| Key | Value | Description |
|------|--------|-------------|
| `maintenance` | true/false | Maintenance state |
| `maintenance_msg` | string | Message from admin |
| `last_change` | datetime | Timestamp of change |

---

## 🔐 8. Railway Bot Shutdown Endpoint (Example)

```python
@app.route('/shutdown', methods=['POST'])
def shutdown():
    token = request.headers.get("Authorization")
    if token != ADMIN_SECRET:
        return "Unauthorized", 401
    os._exit(0)
```

---

## 🧠 9. Auto-Recovery Logic

Cloudflare Worker checks Railway bot every 1 minute:
- If bot responds `200 OK`, set maintenance=false.
- If fails, keep maintenance mode active.

---

## 📊 10. Optional Features
- 🛡️ Trusted User Badge for helpful reporters.
- 🕹️ Bypass whitelist for admin during maintenance.
- 📢 Broadcast notice to all users during maintenance.
- 📈 Mini dashboard showing status, uptime, reports count.

---

## 🎯 11. Goals
- Provide **safe anonymous chatting** experience.  
- Combine **AI-like behavior detection** with **human validation**.  
- Enable **zero-downtime updates** using a **dual-layer system (Worker + Railway)**.

---

## 🧭 12. Summary Flow

```
User → Telegram → Cloudflare Worker
    ↓
If maintenance == false → Forward to Railway Bot
If maintenance == true → Show maintenance message
```

### Inside Railway Bot:

```
Incoming message
  ↓
Filter & Check
  ↓
Behavior Analysis
  ↓
Validation (if needed)
  ↓
Reputation Update
  ↓
Forward to partner / Action (ban, shadowban, etc.)
```

---

## 🧱 13. Advantages of This System

| Category | Benefit |
|-----------|----------|
| ⚡ Performance | Fast response via Worker caching and control |
| 🔒 Security | Railway hidden behind Worker layer |
| 🧠 Intelligence | Behavior + content + validation logic combined |
| 🚀 Scalability | Easy to deploy and maintain |
| 🤝 Trust | Users feel safe and heard |

---

## 📄 End of Documentation
This Markdown file includes full system architecture, user flow, feature design, reputation system, and maintenance management for the Telegram Anonymous Chat Bot.
