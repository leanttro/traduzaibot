import os
import google.generativeai as genai
from flask import Flask, render_template
from flask_socketio import SocketIO, emit
from dotenv import load_dotenv

# --- 1. CONFIGURAÇÃO INICIAL ---
print("ℹ️  Iniciando o TraduzAIBot Server...")
load_dotenv()  # Carrega variáveis do .env

# Configura o Flask e o SocketIO
app = Flask(__name__, template_folder='.', static_folder='.', static_url_path='')
# Permite todas as origens (CORS) para testes fáceis
socketio = SocketIO(app, cors_allowed_origins="*")

# --- 2. CARREGAMENTO DA API KEY (SÓ GEMINI) ---
model = None
try:
    GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
    if not GEMINI_API_KEY:
        print("❌ ERRO CRÍTICO: GEMINI_API_KEY não encontrada no .env")
    else:
        genai.configure(api_key=GEMINI_API_KEY)
        
        # Usando o modelo gemini-2.5-flash-preview-09-2025 conforme recomendado
        model = genai.GenerativeModel('gemini-2.5-flash-preview-09-2025')
        print("✅  Modelo Gemini ('gemini-2.5-flash-preview-09-2025') inicializado.")

except Exception as e:
    print(f"❌ Erro ao inicializar o modelo Gemini: {e}")

# --- 3. ROTA PRINCIPAL DA APLICAÇÃO ---
@app.route('/')
def index():
    """Serve a página principal do chat (index.html)."""
    return render_template('index.html')

# --- 4. HANDLERS DE EVENTOS DO CHAT (A MÁGICA) ---

@socketio.on('connect')
def handle_connect():
    """Chamado quando um novo usuário se conecta."""
    print(f"🔌 Cliente conectado: {request.sid}")
    emit('system_message', {'message': 'Bem-vindo ao TraduzAIBot!'}, room=request.sid)

@socketio.on('disconnect')
def handle_disconnect():
    """Chamado quando um usuário se desconecta."""
    print(f"🔌 Cliente desconectado: {request.sid}")

@socketio.on('send_message')
def handle_chat_message(data):
    """
    Recebe uma mensagem de um usuário, traduz com o Gemini,
    e 'transmite' (emite) para TODOS os usuários conectados.
    """
    if not model:
        print("❌ Erro: Tentativa de enviar mensagem sem o modelo Gemini carregado.")
        emit('chat_error', {'error': 'A API de IA não está inicializada no servidor.'}, room=request.sid)
        return

    # 1. Pega os dados enviados pelo JavaScript
    user_message = data.get('message', '')
    my_lang = data.get('my_lang', 'Português')
    target_lang = data.get('target_lang', 'Inglês')
    username = data.get('username', 'Anônimo')
    
    print(f"💬 Mensagem de '{username}': '{user_message}' (Traduzir de {my_lang} para {target_lang})")

    # 2. Prepara o prompt de tradução para o Gemini
    # Este prompt é direto ao ponto: "apenas traduza"
    translation_prompt = f"""
    Você é um tradutor. Traduza o texto a seguir, do idioma '{my_lang}' para o idioma '{target_lang}'.
    Responda APENAS com o texto traduzido. Não adicione saudações, explicações, contexto ou formatação.
    Se o texto for gíria ou muito informal, tente encontrar o equivalente mais próximo.

    Texto:
    "{user_message}"
    """

    try:
        # 3. Chama a API do Gemini
        response = model.generate_content(
            translation_prompt,
            # Configurações de segurança para evitar bloqueios desnecessários em chat
            safety_settings={
                'HARM_CATEGORY_HATE_SPEECH': 'BLOCK_NONE',
                'HARM_CATEGORY_HARASSMENT': 'BLOCK_NONE',
                'HARM_CATEGORY_SEXUALLY_EXPLICIT': 'BLOCK_NONE',
                'HARM_CATEGORY_DANGEROUS_CONTENT': 'BLOCK_NONE',
            }
        )
        translated_text = response.text.strip()
        print(f"🤖 Tradução: '{translated_text}'")

        # 4. Prepara o pacote de dados para enviar a TODOS os clientes
        message_packet = {
            'username': username,
            'original_message': user_message,
            'original_lang': my_lang,
            'translated_message': translated_text,
            'translated_lang': target_lang,
            'timestamp': datetime.now().strftime('%H:%M')
        }
        
        # 5. Emite a mensagem traduzida para TODOS (broadcast=True)
        # O evento que o JS vai ouvir é 'receive_message'
        emit('receive_message', message_packet, broadcast=True)

    except Exception as e:
        print(f"❌ Erro ao chamar a API do Gemini ou ao emitir: {e}")
        # Emite uma mensagem de erro de volta APENAS para o remetente
        emit('chat_error', {'error': f'Erro ao processar tradução: {e}'}, room=request.sid)


# --- 5. Execução do Servidor ---
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 Servidor Socket.IO rodando em http://localhost:{port}")
    # Usa socketio.run() em vez de app.run() para habilitar WebSockets
    # gevent é um servidor assíncrono recomendado
    # Se 'gevent' não estiver instalado, você pode remover async_mode='gevent'
    try:
        socketio.run(app, host="0.0.0.0", port=port, debug=True, allow_unsafe_werkzeug=True)
    except ImportError:
        print("⚠️  'gevent' não encontrado. Rodando em modo de polling padrão.")
        socketio.run(app, host="0.0.0.0", port=port, debug=True, allow_unsafe_werkzeug=True)