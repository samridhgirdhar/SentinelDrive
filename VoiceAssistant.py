#!/usr/bin/env python3
"""
Sheero - Smart Driver AI Assistant
----------------------------------
Integrates with Driver Safety Suite to provide:
- Voice‑activated AI assistance ("gogi")
- Proactive safety suggestions based on driver state
- Continuous monitoring of driver conditions
- Music, navigation and other assistance features
- Modern animated dashboard interface

Requirements:
- Ollama with mistral model installed
- Python packages: speech_recognition, gtts, pygame, flask, requests, numpy, etc.
"""

import os
import time
import json
import queue
import random
import threading
import subprocess
import numpy as np
import sounddevice as sd
import speech_recognition as sr
import requests
import tempfile
from gtts import gTTS
import pygame
from flask import Flask, request, jsonify, render_template, send_from_directory

global_mic_active = True

# -------------------- CONFIGURATION --------------------
# Voice recognition settings
WAKE_WORD = "gogi"
LISTENING_TIMEOUT = 10  # seconds to listen for command after wake word
SAMPLE_RATE = 16000
BLOCK_SIZE = 8000  # 0.5s at 16kHz

# Ollama settings
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL_NAME = "mistral"

# TTS settings
TTS_LANG = 'en'  # Language for Google TTS

# Safety thresholds for intervention
CONSECUTIVE_ALERTS_THRESHOLD = 2  # Number of alerts before intervention
ALERT_WINDOW = 300  # Consider alerts within this many seconds (5 minutes)

# Communication settings
FLASK_PORT = 8080  # Port for API server to receive alerts
DASHBOARD_PORT = 8081  # Port for dashboard UI

# Music/audio resources
CALM_MUSIC_OPTIONS = [
    "relaxing_melody_1.mp3",
    "calm_piano_2.mp3",
    "nature_sounds_3.mp3",
]

# Directory for static files
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(STATIC_DIR, exist_ok=True)

# -------------------- GLOBALS --------------------
driver_state = {
    "DROWSY": False,
    "DRUNK": False,
    "STRESS": False,
    "STEER": "STRAIGHT",
    "last_alerts": [],  # List of (timestamp, alert_type) tuples
    "continuous_monitoring": True,
    "last_suggestion": 0,  # Timestamp of last suggestion
    "suggestion_cooldown": 60,  # Seconds between suggestions
    "conversation_active": False,
    "crash_detected": False,
    "assistant_state": "standby"  # New: tracks assistant visual state
}

speech_queue = queue.Queue()  # Commands detected by speech recognition
tts_queue = queue.Queue()     # Text to be spoken
alert_queue = queue.Queue()   # Safety alerts from main program

# Create Flask apps for API and Dashboard
api_app = Flask("api")
dashboard_app = Flask("dashboard", 
                     static_folder=STATIC_DIR,
                     template_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates"))

pygame.mixer.init()

# Initialize websocket clients
connected_clients = set()

# -------------------- HELPER FUNCTIONS --------------------
def log(message):
    """Print log message with timestamp"""
    print(f"[{time.strftime('%H:%M:%S')}] {message}")

def query_ollama(prompt, system_prompt=None, temperature=0.7, max_tokens=500):
    """Query the Ollama API with the given prompt"""
    headers = {"Content-Type": "application/json"}
    data = {
        "model": MODEL_NAME,
        "prompt": prompt,
        "stream": False,
        "temperature": temperature,
        "max_tokens": max_tokens
    }
    if system_prompt:
        data["system"] = system_prompt
    try:
        response = requests.post(OLLAMA_URL, headers=headers, json=data)
        if response.status_code == 200:
            return response.json().get("response", "")
        else:
            log(f"Error querying Ollama: {response.status_code} - {response.text}")
            return "Sorry, I'm having trouble thinking right now."
    except Exception as e:
        log(f"Exception when querying Ollama: {e}")
        return "Sorry, I can't access my thinking capabilities at the moment."

def speak(text):
    """Add text to TTS queue"""
    tts_queue.put(text)
    set_assistant_state("speaking")

def set_assistant_state(state):
    """Update assistant state and notify connected clients"""
    driver_state["assistant_state"] = state
    broadcast_state()

def broadcast_state():
    """Send current state to all connected dashboard clients"""
    state_data = {
        "state": driver_state["assistant_state"],
        "alert": {
            "drowsy": driver_state["DROWSY"],
            "drunk": driver_state["DRUNK"],
            "stress": driver_state["STRESS"]
        }
    }
    # Use requests to send to the dashboard's state endpoint
    try:
        requests.post(f"http://localhost:{DASHBOARD_PORT}/update_state", 
                      json=state_data)
    except Exception as e:
        log(f"Error broadcasting state: {e}")

def get_driver_assistance_prompt():
    """Generate a context-aware prompt for the LLM based on driver state"""
    alerts = []
    if driver_state["DROWSY"]:
        alerts.append("drowsiness")
    if driver_state["DRUNK"]:
        alerts.append("possible impairment or unwellness")
    if driver_state["STRESS"]:
        alerts.append("signs of stress")
    recent_alerts_count = sum(1 for ts, _ in driver_state["last_alerts"]
                              if time.time() - ts < ALERT_WINDOW)
    if not alerts:
        return None
    context = f"""
    BE EXTREMELY SHORT IN YOUR RESPONSES. GIVE 1 line answer
    As a driving assistant, I need to help a driver who is showing {' and '.join(alerts)}.
    The driver has had {recent_alerts_count} safety alerts in the past {ALERT_WINDOW//60} minutes.
    Current steering direction: {driver_state['STEER']}

    Provide a brief, helpful suggestion that is:
    1. Calming and supportive in tone
    2. Safety-focused without being judgmental
    3. Actionable (something the driver can do immediately)
    4. Brief (under 20 words if possible)
    """
    return context

def check_alert_threshold():
    """Check if we've hit threshold for intervention"""
    now = time.time()
    driver_state["last_alerts"] = [
        (ts, a) for ts, a in driver_state["last_alerts"] if now - ts < ALERT_WINDOW
    ]
    recent_alerts = len(driver_state["last_alerts"])
    if now - driver_state["last_suggestion"] < driver_state["suggestion_cooldown"]:
        return False
    return recent_alerts >= CONSECUTIVE_ALERTS_THRESHOLD

def play_audio_file(filename):
    """Play an audio file using pygame"""
    try:
        if os.path.exists(filename):
            pygame.mixer.music.load(filename)
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                time.sleep(0.1)
            log(f"Played audio: {filename}")
        else:
            log(f"Audio file not found: {filename}")
    except Exception as e:
        log(f"Error playing audio: {e}")

def play_calm_music():
    """Select and play a calming music track"""
    music_file = random.choice(CALM_MUSIC_OPTIONS)
    speak("Playing some calming music to help you relax.")
    log(f"Would play music: {music_file}")
    # play_audio_file(music_file)

# -------------------- CREATE DASHBOARD HTML --------------------
def setup_dashboard_files():
    """Create necessary files for dashboard UI"""
    os.makedirs(os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates"), exist_ok=True)
    
    # Create dashboard HTML file
    dashboard_html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Sheero - Car Dashboard AI Assistant</title>
        <style>
            body {
                margin: 0;
                padding: 0;
                background-color: #0a0b0c;
                color: #fff;
                font-family: 'Arial', sans-serif;
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
                height: 100vh;
                overflow: hidden;
            }
            
            .dashboard-container {
                width: 100%;
                max-width: 800px;
                position: relative;
            }
            
            .assistant-container {
                position: relative;
                width: 320px;
                height: 320px;
                margin: 0 auto;
                display: flex;
                align-items: center;
                justify-content: center;
            }
            
            .assistant-background {
                position: absolute;
                width: 100%;
                height: 100%;
                background: radial-gradient(circle, rgba(19,41,75,0.8) 0%, rgba(6,11,21,0.5) 100%);
                border-radius: 50%;
                box-shadow: 0 0 50px rgba(42, 95, 233, 0.3);
                transition: box-shadow 0.5s ease;
            }
            
            .assistant-ring {
                position: absolute;
                width: 260px;
                height: 260px;
                border-radius: 50%;
                border: 2px solid rgba(64, 156, 255, 0.5);
                box-shadow: 0 0 20px rgba(64, 156, 255, 0.3);
                transition: box-shadow 0.5s ease;
            }
            
            .assistant-center {
                position: absolute;
                width: 200px;
                height: 200px;
                border-radius: 50%;
                background: radial-gradient(circle, rgba(13,35,67,0.8) 0%, rgba(7,18,34,0.6) 100%);
                display: flex;
                align-items: center;
                justify-content: center;
                overflow: hidden;
            }
            
            .assistant-animation {
                width: 100%;
                height: 100%;
                display: flex;
                align-items: center;
                justify-content: center;
            }
            
            .status-text {
                position: absolute;
                bottom: -40px;
                text-align: center;
                font-size: 16px;
                font-weight: 300;
                color: rgba(255, 255, 255, 0.8);
                text-transform: uppercase;
                letter-spacing: 2px;
            }
            
            .particles {
                position: absolute;
                width: 100%;
                height: 100%;
                pointer-events: none;
            }
            
            .particle {
                position: absolute;
                background: rgba(64, 156, 255, 0.5);
                border-radius: 50%;
                pointer-events: none;
            }
            
            /* Wave Animation */
            .wave-container {
                position: absolute;
                width: 100%;
                height: 100%;
                display: flex;
                align-items: center;
                justify-content: center;
                opacity: 0;
                transition: opacity 0.5s ease;
            }
            
            .wave {
                position: absolute;
                width: 160px;
                height: 40px;
                display: flex;
                justify-content: center;
            }
            
            .wave-bar {
                background: linear-gradient(to top, #409cff, #7bbfff);
                width: 6px;
                height: 100%;
                margin: 0 2px;
                border-radius: 3px;
                animation: wave 1.2s infinite ease-in-out;
            }
            
            @keyframes wave {
                0%, 100% { height: 10px; }
                50% { height: 40px; }
            }
            
            /* Orbit Animation */
            .orbit-container {
                position: absolute;
                width: 100%;
                height: 100%;
                opacity: 0;
                transition: opacity 0.5s ease;
            }
            
            .orbit {
                position: absolute;
                border: 1px solid rgba(64, 156, 255, 0.3);
                border-radius: 50%;
            }
            
            .orbit-particle {
                position: absolute;
                width: 6px;
                height: 6px;
                background: #409cff;
                border-radius: 50%;
                box-shadow: 0 0 10px rgba(64, 156, 255, 0.8);
            }
            
            /* Pulse Animation */
            .pulse-container {
                position: absolute;
                width: 100%;
                height: 100%;
                opacity: 0;
                transition: opacity 0.5s ease;
            }
            
            .pulse-circle {
                position: absolute;
                border: 2px solid rgba(64, 156, 255, 0.5);
                border-radius: 50%;
                width: 60px;
                height: 60px;
                opacity: 0;
                animation: pulse 2s infinite;
            }
            
            @keyframes pulse {
                0% { transform: scale(0.5); opacity: 0.8; }
                100% { transform: scale(2); opacity: 0; }
            }
            
            /* Alert indicators */
            .alert-indicators {
                position: absolute;
                bottom: -70px;
                left: 0;
                right: 0;
                display: flex;
                justify-content: center;
                gap: 20px;
            }
            
            .alert-indicator {
                width: 10px;
                height: 10px;
                border-radius: 50%;
                background-color: #333;
                transition: all 0.3s ease;
            }
            
            .alert-indicator.active {
                background-color: #f55;
                box-shadow: 0 0 10px #f55;
            }
            
            /* Info text */
            .info-text {
                margin-top: 100px;
                text-align: center;
                color: rgba(255, 255, 255, 0.6);
                font-size: 14px;
            }
            
            @keyframes rotate {
                0% { transform: rotate(0deg); }
                100% { transform: rotate(360deg); }
            }
            
            @keyframes orbit1 {
                0% { transform: translate(60px, 0); }
                100% { transform: translate(60px, 0) rotate(360deg); }
            }
            
            @keyframes orbit2 {
                0% { transform: translate(40px, 0) rotate(0deg); }
                100% { transform: translate(40px, 0) rotate(-360deg); }
            }
            
            @keyframes orbit3 {
                0% { transform: translate(20px, 20px); }
                100% { transform: translate(20px, 20px) rotate(360deg); }
            }
            
            @keyframes float {
                0%, 100% { transform: translateY(0) translateX(0); }
                50% { transform: translateY(-10px) translateX(5px); }
            }
        </style>
    </head>
    <body>
        <div class="dashboard-container">
            <div class="assistant-container">
                <div class="assistant-background"></div>
                <div class="assistant-ring"></div>
                <div class="assistant-center">
                    <div class="assistant-animation">
                        <div class="particles" id="particles"></div>
                        
                        <!-- Speaking Animation -->
                        <div class="wave-container" id="speaking-animation">
                            <div class="wave">
                                <div class="wave-bar" style="animation-delay: -1.2s"></div>
                                <div class="wave-bar" style="animation-delay: -1.0s"></div>
                                <div class="wave-bar" style="animation-delay: -0.8s"></div>
                                <div class="wave-bar" style="animation-delay: -0.6s"></div>
                                <div class="wave-bar" style="animation-delay: -0.4s"></div>
                                <div class="wave-bar" style="animation-delay: -0.2s"></div>
                                <div class="wave-bar" style="animation-delay: 0s"></div>
                                <div class="wave-bar" style="animation-delay: -0.2s"></div>
                                <div class="wave-bar" style="animation-delay: -0.4s"></div>
                                <div class="wave-bar" style="animation-delay: -0.6s"></div>
                                <div class="wave-bar" style="animation-delay: -0.8s"></div>
                                <div class="wave-bar" style="animation-delay: -1.0s"></div>
                            </div>
                        </div>
                        
                        <!-- Listening Animation -->
                        <div class="orbit-container" id="listening-animation">
                            <div class="orbit" style="width: 120px; height: 120px; animation: rotate 8s linear infinite"></div>
                            <div class="orbit" style="width: 80px; height: 80px; animation: rotate 5s linear infinite reverse"></div>
                            <div class="orbit-particle" style="animation: orbit1 8s linear infinite"></div>
                            <div class="orbit-particle" style="animation: orbit2 5s linear infinite"></div>
                            <div class="orbit-particle" style="animation: orbit3 3s linear infinite"></div>
                        </div>
                        
                        <!-- Standby Animation -->
                        <div class="pulse-container" id="standby-animation">
                            <div class="pulse-circle" style="animation-delay: 0s"></div>
                            <div class="pulse-circle" style="animation-delay: 0.6s"></div>
                            <div class="pulse-circle" style="animation-delay: 1.2s"></div>
                        </div>
                    </div>
                </div>
                <div class="status-text" id="status-text">Standby</div>
                
                <!-- Alert indicators -->
                <div class="alert-indicators">
                    <div class="alert-indicator" id="drowsy-indicator" title="Drowsiness Alert"></div>
                    <div class="alert-indicator" id="drunk-indicator" title="Impairment Alert"></div>
                    <div class="alert-indicator" id="stress-indicator" title="Stress Alert"></div>
                </div>
            </div>
            
            <div class="info-text">
                Sheero AI Assistant | Say "gogi" to activate
            </div>
        </div>

        <script>
            // Current animation state
            let currentMode = 'standby';
            
            // Get animation elements
            const speakingAnimation = document.getElementById('speaking-animation');
            const listeningAnimation = document.getElementById('listening-animation');
            const standbyAnimation = document.getElementById('standby-animation');
            const statusText = document.getElementById('status-text');
            const particles = document.getElementById('particles');
            
            // Get alert indicators
            const drowsyIndicator = document.getElementById('drowsy-indicator');
            const drunkIndicator = document.getElementById('drunk-indicator');
            const stressIndicator = document.getElementById('stress-indicator');
            
            // Set initial state
            window.onload = function() {
                createParticles();
                setMode('standby');
                
                // Start state polling
                pollState();
            };
            
            // Create background particles
            function createParticles() {
                for (let i = 0; i < 30; i++) {
                    const particle = document.createElement('div');
                    particle.classList.add('particle');
                    
                    // Random size
                    const size = Math.random() * 3 + 1;
                    particle.style.width = size + 'px';
                    particle.style.height = size + 'px';
                    
                    // Random position
                    const x = Math.random() * 100;
                    const y = Math.random() * 100;
                    particle.style.left = x + '%';
                    particle.style.top = y + '%';
                    
                    // Random opacity
                    particle.style.opacity = Math.random() * 0.5 + 0.2;
                    
                    // Animation
                    const duration = Math.random() * 5 + 5;
                    particle.style.animation = `float ${duration}s infinite ease-in-out`;
                    
                    particles.appendChild(particle);
                }
            }
            
            // Set animation mode
            function setMode(mode) {
                if (mode === currentMode) return;
                
                // Hide all animations
                speakingAnimation.style.opacity = '0';
                listeningAnimation.style.opacity = '0';
                standbyAnimation.style.opacity = '0';
                
                // Show selected animation
                setTimeout(() => {
                    if (mode === 'speaking') {
                        speakingAnimation.style.opacity = '1';
                        statusText.textContent = 'Speaking';
                        
                        // Add visual effects for speaking
                        document.querySelector('.assistant-ring').style.boxShadow = '0 0 30px rgba(64, 156, 255, 0.5)';
                        document.querySelector('.assistant-background').style.boxShadow = '0 0 60px rgba(42, 95, 233, 0.4)';
                    } 
                    else if (mode === 'listening') {
                        listeningAnimation.style.opacity = '1';
                        statusText.textContent = 'Listening';
                        
                        // Add visual effects for listening
                        document.querySelector('.assistant-ring').style.boxShadow = '0 0 30px rgba(64, 196, 255, 0.5)';
                        document.querySelector('.assistant-background').style.boxShadow = '0 0 60px rgba(42, 165, 233, 0.4)';
                    } 
                    else {
                        standbyAnimation.style.opacity = '1';
                        statusText.textContent = 'Standby';
                        
                        // Reset visual effects
                        document.querySelector('.assistant-ring').style.boxShadow = '0 0 20px rgba(64, 156, 255, 0.3)';
                        document.querySelector('.assistant-background').style.boxShadow = '0 0 50px rgba(42, 95, 233, 0.3)';
                    }
                    
                    currentMode = mode;
                }, 300);
            }
            
            // Set alert status
            function setAlerts(alerts) {
                drowsyIndicator.classList.toggle('active', alerts.drowsy);
                drunkIndicator.classList.toggle('active', alerts.drunk);
                stressIndicator.classList.toggle('active', alerts.stress);
            }
            
            // Poll for state updates
            function pollState() {
                fetch('/state')
                    .then(response => response.json())
                    .then(data => {
                        setMode(data.state);
                        setAlerts(data.alert);
                    })
                    .catch(error => console.error('Error polling state:', error))
                    .finally(() => {
                        // Poll again after delay
                        setTimeout(pollState, 500);
                    });
            }
        </script>
    </body>
    </html>
    """
    
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates","dashboard.html"), "w") as f:
        f.write(dashboard_html)
    
    log("Dashboard HTML file created")

# -------------------- API ENDPOINTS --------------------
@api_app.route('/alert', methods=['POST'])
def receive_alert():
    """Endpoint to receive alerts from the Driver Safety Suite"""
    try:
        data = request.json
        alert_type = data.get('type')
        state = data.get('state', True)
        log(f"Received alert: {alert_type}, state: {state}")

        # Update driver state and queue
        if alert_type in ["DROWSY", "DRUNK", "STRESS"]:
            driver_state[alert_type] = state
            if state:
                driver_state["last_alerts"].append((time.time(), alert_type))
                alert_queue.put(alert_type)

                # Immediately notify driver with a custom message
                if alert_type == "DROWSY":
                    speak("You seem a bit drowsy. Maybe pull over and rest.")
                elif alert_type == "DRUNK":
                    speak("You seem impaired. Consider stopping driving.")
                elif alert_type == "STRESS":
                    speak("You seem stressed. Take a moment to relax before continuing.")

        elif alert_type == "STEER":
            driver_state["STEER"] = data.get('direction', 'STRAIGHT')

        elif alert_type == "CRASH":
            driver_state["crash_detected"] = True
            alert_queue.put("CRASH")

        return jsonify({"status": "success"})
    except Exception as e:
        log(f"Error processing alert: {e}")
        return jsonify({"status": "error", "message": str(e)}), 400

# -------------------- DASHBOARD ENDPOINTS --------------------
@dashboard_app.route('/')
def dashboard():
    """Serve the dashboard interface"""
    return render_template('dashboard.html')

@dashboard_app.route('/state')
def get_state():
    """Return current assistant state"""
    return jsonify({
        "state": driver_state["assistant_state"],
        "alert": {
            "drowsy": driver_state["DROWSY"],
            "drunk": driver_state["DRUNK"],
            "stress": driver_state["STRESS"]
        }
    })

@dashboard_app.route('/update_state', methods=['POST'])
def update_state():
    """Update the state from API server"""
    try:
        data = request.json
        if 'state' in data:
            driver_state["assistant_state"] = data["state"]
        if 'alert' in data:
            if 'drowsy' in data['alert']:
                driver_state["DROWSY"] = data['alert']['drowsy']
            if 'drunk' in data['alert']:
                driver_state["DRUNK"] = data['alert']['drunk']
            if 'stress' in data['alert']:
                driver_state["STRESS"] = data['alert']['stress']
        return jsonify({"status": "success"})
    except Exception as e:
        log(f"Error updating state: {e}")
        return jsonify({"status": "error", "message": str(e)}), 400

# -------------------- THREAD FUNCTIONS --------------------
def tts_worker():
    """Thread to handle text-to-speech conversion using Google TTS"""
    global global_mic_active
    while True:
        if not tts_queue.empty():
            text = tts_queue.get()
            log(f"Speaking: {text}")
            try:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as f:
                    filename = f.name
                gTTS(text=text, lang=TTS_LANG).save(filename)

                # Mute wake-word listener while speaking
                global_mic_active = False
                set_assistant_state("speaking")

                pygame.mixer.music.load(filename)
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy():
                    time.sleep(0.1)

                # Re-enable wake-word listener
                global_mic_active = True
                set_assistant_state("standby")

                os.remove(filename)
            except Exception as e:
                log(f"Error in TTS: {e}")
                global_mic_active = True
                set_assistant_state("standby")
        else:
            time.sleep(0.1)

def wake_word_detector():
    """Thread to continuously listen for WAKE_WORD, then capture one command."""
    global global_mic_active
    recognizer = sr.Recognizer()
    recognizer.dynamic_energy_threshold = True

    # Ambient noise calibration
    with sr.Microphone() as src:
        recognizer.adjust_for_ambient_noise(src, duration=1)
    log(f"Wake-word detector ready, listening for '{WAKE_WORD}'")

    while True:
        if not global_mic_active:
            time.sleep(0.1)
            continue

        try:
            with sr.Microphone() as src:
                audio = recognizer.listen(src, timeout=2, phrase_time_limit=2)
            text = recognizer.recognize_google(audio).lower()
            log(f"Heard: {text}")

            if WAKE_WORD in text:
                log("Wake word detected!")

                # 1) Mute listener
                global_mic_active = False
                driver_state["conversation_active"] = True
                set_assistant_state("listening")

                # 2) Ask the prompt
                speak("How can I help you?")

                # 3) Wait for TTS to finish (tts_worker will re-enable mic)
                while not global_mic_active:
                    time.sleep(0.1)
                
                # Reset state to listening
                set_assistant_state("listening")

                # 4) Listen once for the actual user command
                with sr.Microphone() as cmd_src:
                    cmd_audio = recognizer.listen(
                        cmd_src,
                        timeout=LISTENING_TIMEOUT,
                        phrase_time_limit=LISTENING_TIMEOUT
                    )
                try:
                    command = recognizer.recognize_google(cmd_audio).lower()
                    log(f"Command: {command}")
                    speech_queue.put(command)
                except sr.UnknownValueError:
                    speak("Sorry, I didn't catch that.")
                except sr.RequestError:
                    speak("Speech service is unavailable right now.")

        except sr.WaitTimeoutError:
            continue
        except sr.UnknownValueError:
            continue
        except Exception as e:
            log(f"Unexpected error in wake-word detector: {e}")
            time.sleep(1)   

def command_processor():
    """Process spoken commands from the user"""
    while True:
        if not speech_queue.empty():
            command = speech_queue.get()
            log(f"Processing command: {command}")

            system_prompt = """
            You are an AI driving assistant. You should:
            1. Provide helpful, concise responses to the driver's queries
            2. Prioritize the driver's safety above all else
            3. Suggest actions that keep the driver's attention on the road
            4. Keep responses brief (1-3 sentences when possible)
            """

            if "play" in command and any(w in command for w in ["calm", "relaxing", "music"]):
                play_calm_music()
            elif "stop" in command and "listening" in command:
                speak("Voice assistant deactivated. Say gogi to reactivate.")
                driver_state["conversation_active"] = False
            elif "weather" in command:
                speak("Currently 36 degrees Celcius in okhla new delhi with sunny skies, expected to hit 40 degrees celcius at peak.")
            elif "distance" in command:
                speak("You are about 20 kilometers far from your destination, estimated time remaining is 45 minutes")
            elif "music" in command:
                speak("Now playing on spotify")
            
            else:
                response = query_ollama(command, system_prompt=system_prompt)
                speak(response)

            driver_state["conversation_active"] = False
        else:
            time.sleep(0.1)


def alert_monitor():
    """Monitor and respond to queued driver safety alerts"""
    while True:
        if not alert_queue.empty():
            alert = alert_queue.get()
            log(f"Processing alert: {alert}")

            if alert == "CRASH":
                speak("I've detected a possible collision. Are you okay? Please respond or I'll call emergency services.")
                continue

            if driver_state["conversation_active"]:
                continue

            if check_alert_threshold():
                prompt = get_driver_assistance_prompt()
                if prompt:
                    suggestion = query_ollama(prompt)
                    speak(suggestion)
                    driver_state["last_suggestion"] = time.time()

        time.sleep(5)
        if not driver_state["continuous_monitoring"]:
            continue
        if all(not driver_state[s] for s in ["DROWSY", "DRUNK", "STRESS", "crash_detected", "conversation_active"]):
            continue
        now = time.time()
        if now - driver_state["last_suggestion"] >= driver_state["suggestion_cooldown"]:
            prompt = get_driver_assistance_prompt()
            if prompt:
                suggestion = query_ollama(prompt)
                speak(suggestion)
                driver_state["last_suggestion"] = now

def start_api_server():
    """Start the Flask server to receive alerts"""
    api_app.run(host='0.0.0.0', port=FLASK_PORT)

def start_dashboard_server():
    dashboard_app.run(host='0.0.0.0', port=DASHBOARD_PORT)

# -------------------- MAIN FUNCTION --------------------
def main():
    setup_dashboard_files()
    log("Starting Smart Driver AI Assistant")
    try:
        response = requests.get("http://localhost:11434/api/tags")
        models = response.json().get("models", [])
        if not any(m["name"] == MODEL_NAME for m in models):
            log(f"Warning: {MODEL_NAME} model not found in Ollama. Install it with:")
            log(f"  ollama pull {MODEL_NAME}")
    except:
        log("Warning: Could not connect to Ollama. Make sure it's running on port 11434")

    try:
        speak("Assistant starting up.")
        threads = [
            threading.Thread(target=tts_worker, daemon=True),
            threading.Thread(target=wake_word_detector, daemon=True),
            threading.Thread(target=command_processor, daemon=True),
            threading.Thread(target=alert_monitor, daemon=True),
            threading.Thread(target=start_api_server, daemon=True),
            threading.Thread(target=start_dashboard_server, daemon=True),
        ]
        for t in threads:
            t.start()

        time.sleep(1)
        log("All systems initialized. Assistant is running.")

        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        log("Shutting down assistant")
        speak("Assistant shutting down. Drive safely.")
    except Exception as e:
        log(f"Error in main thread: {e}")
        import traceback; traceback.print_exc()

if __name__ == "__main__":
    main()
