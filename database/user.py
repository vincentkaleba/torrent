from typing import Optional, List, Dict, Union
from database.base import MongoDB
from model.user import PyObjId, UserDB, UserCreate, DLProgress, Quotas, Stats, UserUpdate
from uuid import UUID, uuid4
from datetime import datetime
import logging

log = logging.getLogger(__name__)

class UserManager:
    def __init__(self, db: MongoDB):
        if not isinstance(db, MongoDB):
            raise ValueError("db must be an instance of MongoDB")
        self.db = db
        self._sub_quotas = {
            "free": Quotas(max_dls=3),
            "trial": Quotas(max_dls=5),
            "bronze": Quotas(max_dls=10),
            "silver": Quotas(max_dls=15, max_speed=10),
            "gold": Quotas(max_dls=25, max_speed=50),
            "platinum": Quotas(max_dls=50, max_speed=100),
            "enterprise": Quotas(max_dls=100, max_speed=None)
        }

    async def _check_connection(self) -> bool:
        """Vérifie et établit la connexion si nécessaire"""
        try:
            if not await self.db.is_connected():
                await self.db.connect()
            return True
        except Exception as e:
            log.error(f"Database connection error: {e}")
            return False

    async def _ensure_indexes(self) -> None:
        """Crée les index nécessaires"""
        if not await self._check_connection():
            raise ConnectionError("Failed to connect to database")

        try:
            await self.db.create_indexes("users", [
                {"key": [("uid", 1)], "unique": True},
                {"key": [("dl_active.did", 1)]},
                {"key": [("sub_tier", 1)]}
            ])
        except Exception as e:
            log.error(f"Failed to create indexes: {e}")
            raise

    async def get_user(self, uid: int) -> Optional[UserDB]:
        """Récupère un utilisateur par son ID"""
        if not await self._check_connection():
            return None

        try:
            data = await self.db.find_document("users", {"uid": uid})
            return UserDB(**data) if data else None
        except Exception as e:
            log.error(f"Failed to get user {uid}: {e}", exc_info=True)
            return None

    async def get_all_users(self) -> List[UserDB]:
        """Récupère tous les utilisateurs de la base"""
        if not await self._check_connection():
            return []

        try:
            # Attendre d'abord la coroutine find_document
            cursor = await self.db.find_document("users", {})

            # Vérifier si c'est un vrai curseur MongoDB
            if hasattr(cursor, 'to_list'):
                users_data = await cursor.to_list(length=None)
            else:
                # Si ce n'est pas un curseur, supposons que c'est déjà une liste
                users_data = cursor if isinstance(cursor, list) else [cursor]

            return [UserDB(**data) for data in users_data]

        except Exception as e:
            log.error(f"Failed to get all users: {e}", exc_info=True)
            return []

    async def create_user(self, user_data: UserCreate) -> Optional[UserDB]:
        """Crée un nouvel utilisateur"""
        if not await self._check_connection():
            return None

        try:
            quotas = self._sub_quotas.get(user_data.sub.value, Quotas())
            user = UserDB(
                **user_data.dict(),
                quotas=quotas,
                stats=Stats(last_active=datetime.now())
            )

            result = await self.db.insert_document("users", user.dict(by_alias=True, exclude={"id"}))
            if not result:
                raise ValueError("User creation failed")

            return user
        except Exception as e:
            log.error(f"Failed to create user: {e}", exc_info=True)
            if "duplicate" in str(e).lower():
                return await self.get_user(user_data.uid)
            return None

    async def update_user(self, uid: int, update_data: UserUpdate) -> bool:
        """Met à jour les informations d'un utilisateur"""
        if not await self._check_connection():
            return False

        try:
            changes = {"updated": datetime.now()}

            if update_data.uname is not None:
                changes["uname"] = update_data.uname

            if update_data.sub is not None:
                changes.update({
                    "sub_tier": update_data.sub,
                    "quotas": self._sub_quotas.get(update_data.sub.value, Quotas()).dict()
                })

            if update_data.settings is not None:
                changes["settings"] = update_data.settings.dict()

            if len(changes) <= 1:
                return False

            return await self.db.update_document("users", {"uid": uid}, changes)
        except Exception as e:
            log.error(f"Failed to update user {uid}: {e}", exc_info=True)
            return False

    async def add_download(self, uid: int, download_data: Dict) -> Union[UUID, None]:
        """Ajoute un téléchargement à l'utilisateur"""
        if not await self._check_connection():
            return None

        try:
            user = await self.get_user(uid)
            if user is None:
                log.warning(f"Utilisateur {uid} non trouvé")
                return None

            if len(user.dl_active) >= user.quotas.max_dls:
                raise ValueError(f"Nombre maximum de téléchargements atteint ({user.quotas.max_dls})")

            donnees_propres = {
                k: v for k, v in download_data.items()
                if k not in ['created', 'updated', 'did']
            }

            # Création du téléchargement
            telechargement = DLProgress(
                did=download_data["did"],
                created=datetime.now(),
                updated=datetime.now(),
                **donnees_propres
            )


            # Mise à jour dans la base de données
            succes = await self.db.update_document(
                "users",
                {"uid": uid},
                {
                    "$push": {"dl_active": telechargement.dict(by_alias=True)},
                    "$set": {"updated": datetime.now()}
                }
            )

            return telechargement.did if succes else None

        except ValueError as e:
            log.warning(f"Échec de validation du téléchargement: {e}")
            raise
        except Exception as e:
            log.error(f"Échec de l'ajout du téléchargement: {e}", exc_info=True)
            return None

    async def remove_download(self, uid: int, download_id: str) -> bool:
        """Supprime un téléchargement de l'utilisateur"""
        if not await self._check_connection():
            return False

        try:
            user = await self.get_user(uid)
            if user is None:
                log.warning(f"Utilisateur {uid} non trouvé")
                return False

            download = next((d for d in user.dl_active if d.did == download_id), None)
            if download is None:
                log.warning(f"Téléchargement {download_id} non trouvé pour l'utilisateur {uid}")
                return False

            return await self.db.update_document(
                "users",
                {"uid": uid},
                {
                    "$pull": {"dl_active": {"did": download_id}},
                    "$set": {"updated": datetime.now()}
                }
            )
        except Exception as e:
            log.error(f"Échec de la suppression du téléchargement: {e}", exc_info=True)
            return False

    async def update_download(self, uid: int, download_id: str, update_data: Dict) -> bool:
        """Met à jour un téléchargement de l'utilisateur"""
        if not await self._check_connection():
            return False

        try:
            user = await self.get_user(uid)
            if user is None:
                log.warning(f"Utilisateur {uid} non trouvé")
                return False

            download = next((d for d in user.dl_active if d.did == download_id), None)
            if download is None:
                log.warning(f"Téléchargement {download_id} non trouvé pour l'utilisateur {uid}")
                return False

            update_data["updated"] = datetime.now()

            return await self.db.update_document(
                "users",
                {"uid": uid, "dl_active.did": download_id},
                {"$set": update_data}
            )
        except Exception as e:
            log.error(f"Échec de la mise à jour du téléchargement: {e}", exc_info=True)
            return False

    async def bulk_update_downloads(self, updates: List[Dict]) -> bool:
        """Met à jour plusieurs téléchargements en une opération"""
        if not await self._check_connection():
            return False

        try:
            operations = [
                {
                    "filter": {"uid": u["uid"], "dl_active.did": u["dl_id"]},
                    "update": {
                        "$set": {
                            "dl_active.$.progress": u["progress"],
                            "dl_active.$.speed": u["speed"],
                            "dl_active.$.updated": datetime.now(),
                            "updated": datetime.now()
                        }
                    }
                } for u in updates if all(k in u for k in ["uid", "dl_id"])
            ]

            if not operations:
                return False

            return await self.db.bulk_write("users", operations)
        except Exception as e:
            log.error(f"Bulk update failed: {e}", exc_info=True)
            return False

    async def complete_download(self, uid: int, download_id: UUID) -> bool:
        """Marque un téléchargement comme terminé"""
        if not await self._check_connection():
            return False

        try:
            async def transaction_callback(session):
                user = await self.get_user(uid)
                if user is None:
                    return False

                download = next((d for d in user.dl_active if d.did == download_id), None)
                if download is None:
                    return False

                download.status = "completed"
                download.updated = datetime.now()

                return await self.db.update_document(
                    "users",
                    {"uid": uid},
                    {
                        "$pull": {"dl_active": {"did": download_id}},
                        "$push": {"dl_done": download.dict(by_alias=True)},
                        "$inc": {
                            "stats.dls": 1,
                            "stats.down": download.size / 1024
                        },
                        "$set": {"updated": datetime.now()}
                    },
                    session=session
                )

            return await self.db.execute_transaction(transaction_callback)
        except Exception as e:
            log.error(f"Failed to complete download: {e}", exc_info=True)
            return False