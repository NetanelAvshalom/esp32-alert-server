import os
from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route("/")
def home():
    return "ESP32 Alert Server is running 🚀 יודה ינרקומן "

@app.route("/alert", methods=["POST"])
def alert():
    data = request.json
    print("Received alert:", data)
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
