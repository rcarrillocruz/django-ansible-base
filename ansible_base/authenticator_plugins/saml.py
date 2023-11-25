import logging

from django.http import HttpResponse, HttpResponseNotFound
from django.urls import re_path
from django.utils.translation import gettext_lazy as _
from onelogin.saml2.errors import OneLogin_Saml2_Error
from rest_framework import serializers
from rest_framework.reverse import reverse
from rest_framework.serializers import ValidationError
from rest_framework.views import View
from social_core.backends.saml import SAMLAuth

from ansible_base.authentication.social_auth import AuthenticatorStorage, AuthenticatorStrategy, SocialAuthMixin
from ansible_base.authenticator_plugins.base import AbstractAuthenticatorPlugin, BaseAuthenticatorConfiguration
from ansible_base.authenticator_plugins.utils import generate_authenticator_slug, get_authenticator_plugin
from ansible_base.models import Authenticator
from ansible_base.serializers.fields import PrivateKey, PublicCert, URLField
from ansible_base.utils.encryption import ENCRYPTED_STRING
from ansible_base.utils.validation import validate_cert_with_key

logger = logging.getLogger('ansible_base.authenticator_plugins.saml')

idp_string = 'IdP'


class SAMLConfiguration(BaseAuthenticatorConfiguration):
    settings_to_enabled_idps_fields = {
        'IDP_URL': 'url',
        'IDP_X509_CERT': 'x509cert',
        'IDP_ENTITY_ID': 'entity_id',
        'IDP_ATTR_EMAIL': 'attr_email',
        'IDP_GROUPS': 'attr_groups',
        'IDP_ATTR_USERNAME': 'attr_username',
        'IDP_ATTR_LAST_NAME': 'attr_last_name',
        'IDP_ATTR_FIRST_NAME': 'attr_first_name',
        'IDP_ATTR_USER_PERMANENT_ID': 'attr_user_permanent_id',
    }

    documentation_url = "https://python-social-auth.readthedocs.io/en/latest/backends/saml.html"

    SP_ENTITY_ID = serializers.CharField(
        allow_null=False,
        max_length=512,
        default="aap_gateway",
        help_text=_(
            "The application-defined unique identifier used as the audience of the SAML service provider (SP) configuration. This is usually the URL for the"
            " service."
        ),
    )
    SP_PUBLIC_CERT = PublicCert(allow_null=False, help_text=_("Create a keypair to use as a service provider (SP) and include the certificate content here."))
    SP_PRIVATE_KEY = PrivateKey(allow_null=False, help_text=_("Create a keypair to use as a service provider (SP) and include the private key content here."))
    ORG_INFO = serializers.JSONField(
        allow_null=False,
        default={"en-US": {"url": "", "name": "", "displayname": ""}},
        help_text=_("Provide the URL, display name, and the name of your app. Refer to the documentation for example syntax."),
    )
    TECHNICAL_CONTACT = serializers.JSONField(
        allow_null=False,
        default={'givenName': "", 'emailAddress': ""},
        help_text=_("Provide the name and email address of the technical contact for your service provider. Refer to the documentation for example syntax."),
    )
    SUPPORT_CONTACT = serializers.JSONField(
        allow_null=False,
        default={'givenName': "", 'emailAddress': ""},
        help_text=_("Provide the name and email address of the support contact for your service provider. Refer to the documentation for example syntax."),
    )
    SP_EXTRA = serializers.JSONField(
        default={"requestedAuthnContext": False},
        help_text=_("A dict of key value pairs to be passed to the underlying python-saml Service Provider configuration setting."),
    )
    SECURITY_CONFIG = serializers.JSONField(
        default={},
        help_text=_(
            "A dict of key value pairs that are passed to the underlying python-saml security setting https://github.com/onelogin/python-saml#settings"
        ),
    )
    EXTRA_DATA = serializers.ListField(
        default=[],
        help_text=_("A list of tuples that maps IDP attributes to extra_attributes. Each attribute will be a list of values, even if only 1 value."),
    )
    IDP_URL = URLField(
        allow_null=False,
        help_text=_("The URL to redirect the user to for login initiation."),
    )
    IDP_X509_CERT = PublicCert(
        allow_null=False,
        help_text=_("The public cert used for secrets coming from the IdP."),
    )
    IDP_ENTITY_ID = serializers.CharField(
        allow_null=False,
        help_text=_("The entity ID returned in the assertion."),
    )
    IDP_GROUPS = serializers.CharField(
        allow_null=True,
        required=False,
        help_text=_("The field in the assertion which represents the users groups."),
    )
    IDP_ATTR_EMAIL = serializers.CharField(
        allow_null=False,
        help_text=_("The field in the assertion which represents the users email."),
    )
    IDP_ATTR_USERNAME = serializers.CharField(
        allow_null=True,
        required=False,
        help_text=_("The field in the assertion which represents the users username."),
    )
    IDP_ATTR_LAST_NAME = serializers.CharField(
        allow_null=False,
        help_text=_("The field in the assertion which represents the users last name."),
    )
    IDP_ATTR_FIRST_NAME = serializers.CharField(
        allow_null=False,
        help_text=_("The field in the assertion which represents the users first name."),
    )
    IDP_ATTR_USER_PERMANENT_ID = serializers.CharField(
        allow_null=True,
        required=False,
        help_text=_("The field in the assertion which represents the users permanent id (overrides IDP_ATTR_USERNAME)"),
    )
    CALLBACK_URL = URLField(
        required=False,
        allow_null=True,
        help_text=_(
            '''Register the service as a service provider (SP) with each identity provider (IdP) you have configured.'''
            '''Provide your SP Entity ID and this ACS URL for your application.'''
        ),
    )

    def validate(self, attrs):
        # attrs is only the data in the configuration field
        errors = {}
        # pull the cert_info out of the existing object (if we have one)
        cert_info = {
            "SP_PRIVATE_KEY": getattr(self.instance, 'configuration', {}).get('SP_PRIVATE_KEY', None),
            "SP_PUBLIC_CERT": getattr(self.instance, 'configuration', {}).get('SP_PUBLIC_CERT', attrs.get('SP_PUBLIC_CERT', None)),
        }

        # Now get the SP_PRIVATE_KEY out of the passed in attrs (if there is any)
        private_key = attrs.get('SP_PRIVATE_KEY', None)
        if private_key and private_key != ENCRYPTED_STRING:
            # We got an input form the attrs so let that override whatever was in the object
            cert_info['SP_PRIVATE_KEY'] = private_key
        # If we didn't get an input or we got ENCRYPTED_STRING but there is an item, we will just use whatever we got from the item

        # If we made it here the cert_info has one of three things:
        #  * None (error state or not passed in on PUT)
        #  * The existing value from the instance
        #  * A new value

        # Now validate that we can load the cert and key and that they match.
        # Technically, we are also doing this on save even if both values came from the existing instance
        # so there is an inefficiency here but it should be trivial
        try:
            validate_cert_with_key(cert_info['SP_PUBLIC_CERT'], cert_info['SP_PRIVATE_KEY'])
        except ValidationError as e:
            errors['SP_PRIVATE_KEY'] = e

        idp_data = attrs.get('ENABLED_IDPS', {}).get(idp_string, {})
        if not idp_data.get('attr_user_permanent_id', None) and not idp_data.get('attr_username'):
            errors['IDP_ATTR_USERNAME'] = "Either IDP_ATTR_USERNAME or IDP_ATTR_USER_PERMANENT_ID needs to be set"

        if errors:
            raise serializers.ValidationError(errors)

        response = super().validate(attrs)
        return response

    def to_internal_value(self, data):
        resp = super().to_internal_value(data)
        idp_data = {}
        for field, idp_field in self.settings_to_enabled_idps_fields.items():
            if field in resp:
                idp_data[idp_field] = resp[field]
                del resp[field]
        resp['ENABLED_IDPS'] = {idp_string: idp_data}
        return resp

    def to_representation(self, configuration):
        if 'ENABLED_IDPS' in configuration:
            for config_setting_name in self.settings_to_enabled_idps_fields:
                enabled_idp_field_name = self.settings_to_enabled_idps_fields[config_setting_name]
                if enabled_idp_field_name in configuration['ENABLED_IDPS'][idp_string]:
                    configuration[config_setting_name] = configuration['ENABLED_IDPS'][idp_string][enabled_idp_field_name]
            del configuration['ENABLED_IDPS']
        return configuration


class AuthenticatorPlugin(SocialAuthMixin, SAMLAuth, AbstractAuthenticatorPlugin):
    configuration_class = SAMLConfiguration
    type = "SAML"
    logger = logger
    category = "sso"
    configuration_encrypted_fields = ['SP_PRIVATE_KEY']

    def get_login_url(self, authenticator):
        url = reverse('social:begin', kwargs={'backend': authenticator.slug})
        return f'{url}?idp={idp_string}'

    def add_related_fields(self, request, authenticator):
        return {"metadata": reverse('authenticator-metadata', kwargs={'pk': authenticator.id})}

    def validate(self, serializer, data):
        # if we have an instance already and we didn't get a configuration parameter we are just updating other fields and can return
        if serializer.instance and 'configuration' not in data:
            return data

        configuration = data['configuration']
        if not configuration.get('CALLBACK_URL', None):
            if not serializer.instance:
                slug = generate_authenticator_slug(data['type'], data['name'])
            else:
                slug = serializer.instance.slug

            configuration['CALLBACK_URL'] = reverse('social:complete', request=serializer.context['request'], kwargs={'backend': slug})

        return data


class SAMLMetadataView(View):
    def get(self, request, pk=None, format=None):
        authenticator = Authenticator.objects.get(id=pk)
        plugin = get_authenticator_plugin(authenticator.type)
        if plugin.type != 'SAML':
            logger.debug(f"Authenticator {authenticator.id} has a type which does not support metadata {plugin.type}")
            return HttpResponseNotFound()

        strategy = AuthenticatorStrategy(AuthenticatorStorage())
        complete_url = authenticator.configuration.get('CALLBACK_URL')
        saml_backend = strategy.get_backend(slug=authenticator.slug, redirect_uri=complete_url)
        try:
            metadata, errors = saml_backend.generate_metadata_xml()
        except OneLogin_Saml2_Error as e:
            errors = e
        if not errors:
            return HttpResponse(content=metadata, content_type='text/xml')
        else:
            return HttpResponse(content=errors, content_type='text/plain')


urls = [
    # SAML Metadata
    re_path(r'authenticators/(?P<pk>[0-9]+)/metadata/$', SAMLMetadataView.as_view(), name='authenticator-metadata'),
]
