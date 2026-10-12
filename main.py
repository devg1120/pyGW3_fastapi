import os
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Request, Depends, Form, File, UploadFile, HTTPException, status
from fastapi.responses import RedirectResponse, HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates

from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship
from passlib.context import CryptContext
from starlette.middleware.sessions import SessionMiddleware

from fastapi import WebSocket, WebSocketDisconnect
from typing import List


# --- アプリケーション初期設定 ---
app = FastAPI()
# Flaskの SECRET_KEY に相当するセッションミドルウェア
app.add_middleware(SessionMiddleware, secret_key="your-secret-key-here")

UPLOAD_FOLDER = 'uploads'
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# テンプレートエンジンの設定
templates = Jinja2Templates(directory="templates")

"""
# Flaskのフラッシュメッセージ（flash）をFastAPIで再現するヘルパー関数
def get_flashed_messages(request: Request, with_categories: bool = True):
    messages = request.session.pop("_flashes", [])
    if with_categories:
        return messages
    return [msg for msg in messages]

def flash(request: Request, message: str, category: str = "info"):
    if "_flashes" not in request.session:
        request.session["_flashes"] = []
    request.session["_flashes"].append((category, message))

# Jinja2のテンプレート内で get_flashed_messages() をそのまま呼べるように登録
templates.env.globals.update(get_flashed_messages=get_flashed_messages)
"""

# Flaskのフラッシュメッセージ（flash）をFastAPIで再現するヘルパー関数
def get_flashed_messages_context(request: Request):
    """HTMLテンプレート側から引数なしで呼べるようにするためのラッパー関数"""
    def _get_flashed_messages(with_categories: bool = True):
        # セッションからメッセージを取得
        messages = request.session.pop("_flashes", [])
        if with_categories:
            return messages
        return [msg for msg in messages]
    return _get_flashed_messages

def flash(request: Request, message: str, category: str = "info"):
    if "_flashes" not in request.session:
        request.session["_flashes"] = []
    request.session["_flashes"].append((category, message))

# 各リクエスト処理時に、テンプレート環境へ request に紐づいた get_flashed_messages を自動注入する設定
@app.middleware("http")
async def add_flash_to_context(request: Request, call_next):
    # テンプレートのグローバル変数に、現在のリクエスト専用の関数をセット
    templates.env.globals["get_flashed_messages"] = get_flashed_messages_context(request)
    response = await call_next(request)
    return response


# --- データベース設定 & モデル定義 ---
# Flaskコードにあったsqlite:///groupware.dbをベースに接続設定
SQLALCHEMY_DATABASE_URL = "sqlite:///./groupware.db"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "user"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False)
    password_hash = Column(String(128), nullable=False)
    
    posts = relationship('Post', back_populates='author')
    schedules = relationship('Schedule', back_populates='creator')

class Post(Base):
    __tablename__ = "post"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(100), nullable=False)
    content = Column(Text, nullable=False)
    date_posted = Column(DateTime, nullable=False, default=datetime.utcnow)
    filename = Column(String(200), nullable=True)
    user_id = Column(Integer, ForeignKey('user.id'), nullable=False)

    author = relationship('User', back_populates='posts')

class Schedule(Base):
    __tablename__ = "schedule"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    date = Column(String(10), nullable=False)  # YYYY-MM-DD
    user_id = Column(Integer, ForeignKey('user.id'), nullable=False)

    creator = relationship('User', back_populates='schedules')

class KanbanTask(Base):
    __tablename__ = "kanban_task"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(100), nullable=False)
    status = Column(String(20), nullable=False, default='todo')

class Document(Base):
    __tablename__ = "document"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    filename = Column(String(200), nullable=False)
    uploaded_at = Column(DateTime, nullable=False, default=datetime.utcnow)

# --- 1. [追加] チャット用のデータベースモデル定義 ---
class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), nullable=False)
    text = Column(Text, nullable=False)
    timestamp = Column(DateTime, nullable=False, default=datetime.utcnow)

# ※これより下の行に Base.metadata.create_all(bind=engine) があれば自動でテーブルが作られます。


# --- 2. リアルタイム通信の管理クラス ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            await connection.send_json(message)

# --- Markdownドキュメント管理用モデル ---
class MarkdownDocument(Base):
    __tablename__ = "markdown_documents"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(100), nullable=False)
    content = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    user_id = Column(Integer, ForeignKey('user.id'), nullable=False)

    # 誰が作成・更新したかわかるようにリレーションを設定
    # (Userモデル側にも relationship を追加するとより便利です)

############################################################################################
# データベースのテーブル自動生成
Base.metadata.create_all(bind=engine)
############################################################################################


# --- 認証 & セキュリティ・共通処理関連 ---
# パスワードハッシュ化用のコンテキスト設定
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def get_db():
    """データベースセッションを管理する依存関係"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_current_user(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    """セッションからログイン中のユーザーを取得します（未ログインならNone）"""
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.query(User).filter(User.id == int(user_id)).first()

def login_required(request: Request, db: Session = Depends(get_db)):
    """ログインを必須にする依存関係。未ログインならログイン画面へリダイレクト"""
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"}
        )
    return user

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """FastAPIでHTTPExceptionを用いたリダイレクトを有効化するハンドラー"""
    if exc.status_code == status.HTTP_303_SEE_OTHER:
        return RedirectResponse(url=exc.headers.get("Location"), status_code=status.HTTP_303_SEE_OTHER)
    return HTMLResponse(content=exc.detail, status_code=exc.status_code)

# --- 画面ルーティング ---

@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    """ダッシュボード（メインページ）"""
    current_user = get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
        
    posts = db.query(Post).order_by(Post.date_posted.desc()).all()
    schedules = db.query(Schedule).order_by(Schedule.date.asc()).all()
    documents = db.query(Document).order_by(Document.uploaded_at.desc()).all()
    
    return templates.TemplateResponse(
      request=request,
      name="index.html",
      context={
        "current_user": current_user,
        "posts": posts,
        "schedules": schedules,
        "documents": documents
      }
    )



@app.get("/members", response_class=HTMLResponse)
def members_page(request: Request, db: Session = Depends(get_db), current_user: User = Depends(login_required)):
    """メンバー管理ページ"""
    members = db.query(User).order_by(User.username.asc()).all()
    return templates.TemplateResponse(
        request=request,
        name="members.html",
        context={
          "current_user": current_user,
          "members": members
        }
    )



@app.get("/kanban", response_class=HTMLResponse)
def kanban(request: Request, db: Session = Depends(get_db), current_user: User = Depends(login_required)):
    """かんばんボードページ"""
    kanban_tasks = db.query(KanbanTask).all()
    return templates.TemplateResponse(
       request=request,
       name="kanban.html",
       context={
         "current_user": current_user,
         "kanban_tasks": kanban_tasks
       }
    )


@app.get("/register", response_class=HTMLResponse)
def register_form(request: Request):
    """新規ユーザー登録画面"""
    return templates.TemplateResponse(request=request, name="register.html")



@app.post("/register")
def register(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    """新規ユーザー登録処理"""
    user_exists = db.query(User).filter(User.username == username).first()
    if user_exists:
        flash(request, 'このユーザー名は既に存在します。', 'danger')
        return RedirectResponse(url="/register", status_code=status.HTTP_303_SEE_OTHER)
        
    hashed_password = pwd_context.hash(password)
    new_user = User(username=username, password_hash=hashed_password)
    db.add(new_user)
    db.commit()
    flash(request, 'アカウント作成が完了しました。ログインしてください。', 'success')
    return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    """ログイン画面"""
    return templates.TemplateResponse(request=request, name="login.html")



@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    """ログイン処理"""
    user = db.query(User).filter(User.username == username).first()
    if user and pwd_context.verify(password, user.password_hash):
        request.session["user_id"] = user.id
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    else:
        flash(request, 'ユーザー名またはパスワードが正しくありません。', 'danger')
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/logout")
def logout(request: Request):
    """ログアウト処理"""
    request.session.clear()
    return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
# --- データ追加・各種アクション ---

@app.post("/kanban/new")
def new_kanban_task(
    request: Request, 
    title: str = Form(...), 
    db: Session = Depends(get_db), 
    current_user: User = Depends(login_required)
):
    """かんばんタスクの追加"""
    if title:
        task = KanbanTask(title=title, status='todo')
        db.add(task)
        db.commit()
        flash(request, 'タスクを追加しました。', 'success')
    return RedirectResponse(url="/kanban", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/post/new")
async def new_post(
    request: Request, 
    title: str = Form(...), 
    content: str = Form(...), 
    file: Optional[UploadFile] = File(None), 
    db: Session = Depends(get_db), 
    current_user: User = Depends(login_required)
):
    """掲示板への投稿（ファイル添付対応）"""
    filename = None
    if file and file.filename != '':
        # Werkzeugのsecure_filenameに代わり、安全なファイル名のみを抽出
        filename = os.path.basename(file.filename)
        file_path = os.path.join(UPLOAD_FOLDER, filename)
        with open(file_path, "wb") as f:
            f.write(await file.read())
            
    post = Post(title=title, content=content, filename=filename, user_id=current_user.id)
    db.add(post)
    db.commit()
    flash(request, '掲示板に投稿しました。', 'success')
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/uploads/{filename}")
def uploaded_file(filename: str, current_user: User = Depends(login_required)):
    """アップロードされたファイルのダウンロード/表示ルーティング"""
    file_path = os.path.join(UPLOAD_FOLDER, filename)
    if os.path.exists(file_path):
        return FileResponse(file_path)
    raise HTTPException(status_code=404, detail="File not found")

@app.post("/schedule/new")
def new_schedule(
    request: Request, 
    title: str = Form(...), 
    description: Optional[str] = Form(None), 
    date: str = Form(...), 
    db: Session = Depends(get_db), 
    current_user: User = Depends(login_required)
):
    """スケジュールの登録（詳細入力欄あり）"""
    schedule = Schedule(title=title, description=description, date=date, user_id=current_user.id)
    db.add(schedule)
    db.commit()
    flash(request, 'スケジュールを登録しました。', 'success')
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/add_schedule")
def add_schedule(
    request: Request, 
    title: str = Form(...), 
    date: str = Form(...), 
    db: Session = Depends(get_db), 
    current_user: User = Depends(login_required)
):
    """スケジュールの登録（簡易フォーム）"""
    if title and date:
        try:
            # 日付文字列のバリデーションチェックをおこない、格納
            date_obj = datetime.strptime(date, '%Y-%m-%d').date()
            new_schedule = Schedule(title=title, date=str(date_obj), user_id=current_user.id)
            db.add(new_schedule)
            db.commit()
        except ValueError:
            flash(request, '日付の形式が不正です。', 'danger')
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/document/upload")
async def upload_document(
    request: Request, 
    file: UploadFile = File(...), 
    db: Session = Depends(get_db), 
    current_user: User = Depends(login_required)
):
    """共有ドキュメントのアップロード"""
    if file and file.filename != '':
        filename = os.path.basename(file.filename)
        file_path = os.path.join(UPLOAD_FOLDER, filename)
        with open(file_path, "wb") as f:
            f.write(await file.read())
            
        doc = Document(name=file.filename, filename=filename)
        db.add(doc)
        db.commit()
        flash(request, 'ドキュメントをアップロードしました。', 'success')
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

# --- JSON API ルーティング ---

@app.get("/api/schedules")
def api_schedules(db: Session = Depends(get_db), current_user: User = Depends(login_required)):
    """カレンダー連携用スケジュール一覧API（JSONを返却）"""
    schedules = db.query(Schedule).all()
    events = []
    for s in schedules:
        events.append({
            'id': s.id,
            'title': s.title,
            'start': str(s.date)
        })
    return events



manager = ConnectionManager()


# --- 3. 画面表示ルーティング（過去ログ読み込み付き） ---
@app.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request, db: Session = Depends(get_db), current_user: User = Depends(login_required)):
    # ページを開いたときに過去50件のメッセージを取得して古い順に並び替える
    past_messages = (
        db.query(ChatMessage)
        .order_by(ChatMessage.timestamp.desc())
        .limit(50)
        .all()
    )
    past_messages.reverse()  # 古い順に戻す

    return templates.TemplateResponse(
        request=request,
        name="chat.html",
        context={
            "current_user": current_user,
            "past_messages": past_messages
        }
    )


# --- 4. WebSocketのエンドポイント ---
@app.websocket("/ws/chat/{username}")
async def websocket_endpoint(websocket: WebSocket, username: str, db: Session = Depends(get_db)):
    await manager.connect(websocket)
    await manager.broadcast({"user": "System", "text": f"◆ {username} さんが参加しました"})

    try:
        while True:
            # フロントエンドからテキストを受信
            data = await websocket.receive_text()
            if not data.strip():
                continue

            # データベースへメッセージを保存
            msg_record = ChatMessage(username=username, text=data)
            db.add(msg_record)
            db.commit()

            # 全員にリアルタイム配信
            await manager.broadcast({"user": username, "text": data})

    except WebSocketDisconnect:
        manager.disconnect(websocket)
        await manager.broadcast({"user": "System", "text": f"◇ {username} さんが退室しました"})


# --- Markdownドキュメント管理 ルーティング ---

@app.get("/markdown", response_class=HTMLResponse)
def markdown_list(request: Request, db: Session = Depends(get_db), current_user: User = Depends(login_required)):
    """ドキュメント管理（サイドバー一覧 ＆ プレビュー・編集統合画面）"""
    docs = db.query(MarkdownDocument).order_by(MarkdownDocument.updated_at.desc()).all()
    return templates.TemplateResponse(
        request=request,
        name="markdown_list.html",  # このテンプレートに全ての機能を統合します
        context={"current_user": current_user, "docs": docs}
    )

# 【変更】従来の @app.get("/markdown/edit") フォーム表示用のルーティングは不要になったため削除します。


@app.post("/markdown/save")
def markdown_save(
    request: Request,
    title: str = Form(...),
    content: str = Form(...),
    doc_id: Optional[int] = Form(None), # 隠しフィールドから送信されるID（新規なら空白）
    db: Session = Depends(get_db),
    current_user: User = Depends(login_required)
):
    """保存・更新処理"""
    if doc_id:
        # 既存ドキュメントの編集（更新）
        doc = db.query(MarkdownDocument).filter(MarkdownDocument.id == doc_id).first()
        if doc:
            doc.title = title
            doc.content = content
            doc.updated_at = datetime.utcnow()
            flash(request, 'ドキュメントを更新しました。', 'success')
    else:
        # 新規作成
        doc = MarkdownDocument(title=title, content=content, user_id=current_user.id)
        db.add(doc)
        db.flush() # コミット前にIDを確定させる
        doc_id = doc.id # 新規作成されたドキュメントのIDを取得
        flash(request, '新しいドキュメントを作成しました。', 'success')

    db.commit()

    # 【修正】リダイレクト先のURLに、閲覧中のドキュメントIDとreadonlyモードのパラメータを付与する
    # ※一覧画面（/markdown）の作りに合わせて、必要なら ?id={doc_id} などを調整してください
    redirect_url = f"/markdown?id={doc_id}&mode=readonly"
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)

# 【変更】JavaScriptから簡単にリクエストを送りやすくするため、URLをPOSTパラメータ対応、または従来のままで受け付けます
@app.post("/markdown/delete/{doc_id}")
def markdown_delete(doc_id: int, request: Request, db: Session = Depends(get_db), current_user: User = Depends(login_required)):
    """削除処理"""
    doc = db.query(MarkdownDocument).filter(MarkdownDocument.id == doc_id).first()
    if doc:
        db.delete(doc)
        db.commit()
        flash(request, 'ドキュメントを削除しました。', 'success')
    return RedirectResponse(url="/markdown", status_code=status.HTTP_303_SEE_OTHER)




# --- サーバー起動設定 ---
if __name__ == '__main__':
    import uvicorn
    # 開発用サーバーをローカルホストの8000番ポートで起動
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
